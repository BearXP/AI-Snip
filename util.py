import base64
import json
import sys
import os
from mimetypes import guess_type
from typing import Optional, Type

import ollama
from openai import AzureOpenAI, OpenAI
from openai._base_client import BaseClient
from openai.types.chat import ParsedChatCompletionMessage
from pydantic import BaseModel


# Source: https://learn.microsoft.com/en-us/azure/ai-services/openai/how-to/gpt-with-vision?tabs=rest
# Function to encode a local image into data URL
def local_image_to_data_url(image_path):
    # Guess the MIME type of the image based on the file extension
    mime_type, _ = guess_type(image_path)
    if mime_type is None:
        mime_type = "application/octet-stream"  # Default MIME type if none is found

    # Read and encode the image file
    with open(image_path, "rb") as image_file:
        base64_encoded_data = base64.b64encode(image_file.read()).decode("utf-8")

    # Construct the data URL
    return f"data:{mime_type};base64,{base64_encoded_data}"


def human_readable_parse(messages: list[dict[str, str]]):
    return "\n".join([f'{msg["role"]}:\n{msg["content"]}' for msg in messages])


def resource_path(relative_path):
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")

    return os.path.join(base_path, relative_path)

def stream_wrapper(stream):
    for response in stream:
        yield response.choices[0].delta.content


class ModelWrapper:
    def __init__(
        self, client: BaseClient, model_name: str, log_file: Optional[str] = None
    ):
        self.client = client
        self.model_name = model_name
        self.log_file = log_file
        self.stats = {"requests": 0, "input_tokens": 0, "completion_tokens": 0}

    def complete(self, messages: list[dict[str, str]], **kwargs) -> str:
        response = self.client.chat.completions.create(
            model=self.model_name, messages=messages, **kwargs
        )
        self.stats["requests"] += 1
        self.stats["input_tokens"] += response.usage.prompt_tokens
        self.stats["completion_tokens"] += response.usage.completion_tokens
        if self.log_file is not None:
            msg_copy = messages.copy()
            msg_copy.append(response.choices[0].message.dict())
            with open(self.log_file, "a") as f:
                f.write(json.dumps(msg_copy) + ",\n")
        return response.choices[0].message.content
    
    def stream_complete(self, messages: list[dict[str, str]], **kwargs) -> str:
        response = self.client.chat.completions.create(
            model=self.model_name, messages=messages, stream=True, **kwargs
        )
        return stream_wrapper(response)
            
    def structured_complete(
        self, messages: list[dict[str, str]], structure_class: Type, **kwargs
    ) -> ParsedChatCompletionMessage:
        response = self.client.beta.chat.completions.parse(
            model=self.model_name,
            messages=messages,
            response_format=structure_class,
        )
        self.stats["requests"] += 1
        self.stats["input_tokens"] += response.usage.prompt_tokens
        self.stats["completion_tokens"] += response.usage.completion_tokens
        if self.log_file is not None:
            msg_copy = messages.copy()
            msg_copy.append(response.choices[0].message.dict())
            with open(self.log_file, "a") as f:
                f.write(json.dumps(msg_copy) + ",\n")

        return response.choices[0].message

    def compute_cost(
        self,
        input_token_cost: Optional[float] = None,
        output_token_cost: Optional[float] = None,
    ) -> float:
        if input_token_cost is None:
            assert output_token_cost is None
            if "gpt-4o" in self.model_name:
                if "mini" in self.model_name:
                    input_token_cost = 0.000165 / 1000
                    output_token_cost = 0.00066 / 1000
                else:
                    if "2024-08-06" in self.model_name:
                        input_token_cost = 0.0025 / 1000
                        output_token_cost = 0.010 / 1000
                    else:
                        input_token_cost = 0.005 / 1000
                        output_token_cost = 0.015 / 1000
            elif "gpt-35" in self.model_name:
                input_token_cost = 0.0005 / 1000
                output_token_cost = 0.0015 / 1000
            else:
                raise ValueError(
                    f"Unknown model name: {self.model_name}. Please provide"
                    " input_token_cost and output_token_cost."
                )

        total_cost = (
            self.stats["input_tokens"] * input_token_cost
            + self.stats["completion_tokens"] * output_token_cost
        )
        return total_cost


class OpenAIModelWrapper(ModelWrapper):
    def __init__(
        self,
        model_name: str = "gpt-4o",
        log_file=None,
        api_key=os.environ.get("OPENAI_API_KEY"),
    ):
        self.client = OpenAI(
            api_key=api_key,
        )
        super().__init__(self.client, model_name, log_file)


class AzureModelWrapper(ModelWrapper):
    def __init__(
        self,
        model_name: str = "gpt-4o",
        log_file=None,
        api_version="2024-08-01-preview",
    ):
        self.client = AzureOpenAI(
            api_key=os.environ.get("AZURE_OPENAI_API_KEY"),
            api_version=api_version,
            azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
        )
        super().__init__(self.client, model_name, log_file)


class OllamaModelWrapper(ModelWrapper):
    def __init__(self, model_name : str = "minicpm-v", log_file = None, api_version = ""):
        self.client = ollama.Client(
            host="http://localhost:11434"
        )
        super().__init__(self.client, model_name, log_file)
    
    def _ollama_reformat_messages(self, messages: list[dict[str, str]]) -> list[dict[str, str]]:
        o_messages = []
        for msg_raw in messages:
            msg = {m['type'] : m[m['type']] for m in msg_raw['content']}
            o_msg = {
                'role' : msg_raw['role'],
                'content' : msg['text']
            }
            if 'image_url' in msg:
                o_msg['images'] = [msg['image_url']['url'][22:]]
            o_messages.append(o_msg)
        return o_messages
    
    def _openai_reformat_messages(self, ollama_msg: dict) -> dict:
        openai_msg = {
            "role" : ollama_msg["role"],
            "content" : {
                "type" : "text",
                "text": ollama_msg["content"]
            }
        }
        return openai_msg

    def complete(self, messages: dict[str, str], **kwargs) -> str:
        ollama_messages = self._ollama_reformat_messages(messages)
        response = self.client.chat(
            model=self.model_name,
            messages=ollama_messages,
            **kwargs
        )
        resp_msg = response['message']
        resp_content = resp_msg['content']
        self.stats["requests"] += 1
        self.stats["input_tokens"] += response['eval_count']
        self.stats["completion_tokens"] += response['eval_count']
        if self.log_file is not None:
            msg_copy = messages.copy()
            msg_copy.append(self._openai_reformat_messages(resp_msg))
            with open(self.log_file, "a") as f:
                f.write(json.dumps(msg_copy) + ",\n")
        return resp_content
    
    def stream_complete(self, messages: list[dict[str, str]], **kwargs) -> str:
        ollama_messages = self._ollama_reformat_messages(messages)
        stream = self.client.chat(
            model=self.model_name,
            messages=ollama_messages,
            stream=True,
            **kwargs
        )
        for chunk in stream:
            yield chunk['message']['content']