import json
import os
import time
from dotenv import load_dotenv
from openai import AsyncOpenAI, OpenAI

from bolna.constants import PRE_FUNCTIONAL_CALL_MESSAGE
from bolna.helpers.function_calling_helpers import trigger_api
from bolna.helpers.utils import convert_to_request_log
from .base_agent import BaseAgent
from bolna.helpers.logger_config import configure_logger

load_dotenv()
logger = configure_logger(__name__)


class OpenAIAssistantAgent(BaseAgent):
    def __init__(self, assistant_id, buffer_size = 100, max_tokens = 10, **kwargs):
        super().__init__()
        self.custom_tools = kwargs.get("api_tools", None)
        logger.info(f"API Tools {self.custom_tools}")
        if self.custom_tools is not None:
            self.trigger_function_call = True
            self.api_params = self.custom_tools['tools_params']
            logger.info(f"Function dict {self.api_params}")
            self.tools = self.custom_tools['tools']
        else:
            self.trigger_function_call = False
        
        self.buffer_size = buffer_size
        self.max_tokens = max_tokens

        llm_key = kwargs.get('llm_key', os.getenv('OPENAI_API_KEY'))
        self.assistant_id = assistant_id
        if self.assistant_id:
            logger.info(f"Initializing OpenAI assistant with assistant id {self.assistant_id}")
            self.openai = OpenAI(api_key=llm_key)
            self.thread_id = self.openai.beta.threads.create().id
            self.model_args = {"max_completion_tokens": self.max_tokens}
            my_assistant = self.openai.beta.assistants.retrieve(self.assistant_id)
            if my_assistant.tools is not None:
                self.tools = [i for i in my_assistant.tools if i.type == "function"]
            logger.info(f'thread id : {self.thread_id}')

        self.async_client = AsyncOpenAI(api_key=llm_key)
        self.started_streaming = False

    async def generate(self, message, synthesize=False, meta_info=None):
        async for token in self.generate_assistant_stream(message[-1]['content'], synthesize=synthesize, meta_info=meta_info):
            logger.info('Agent: {}'.format(token))
            yield token
    
    async def generate_assistant_stream(self, message, synthesize=True, request_json=False, meta_info=None):
        if len(message) == 0:
            raise Exception("No messages provided")


        answer, buffer, resp, called_fun, api_params, i = "", "", "", "", "", 0
        logger.info(f"request to open ai {message} max tokens {self.max_tokens} ")
        model_args = {}
        model_args["thread_id"] = self.thread_id
        model_args["assistant_id"] = self.assistant_id
        model_args["stream"] = True
        logger.info(f"request to open ai with thread {self.thread_id} & asst. id {self.assistant_id}")

        latency = False
        start_time = time.time()
        textual_response = False

        if self.trigger_function_call:
            tools = self.tools

        runs = await self.async_client.beta.threads.runs.list(thread_id=model_args["thread_id"])
        if runs.data and runs.data[0].status in ["in_progress", "queued", "requires_action"]:
            await self.async_client.beta.threads.runs.cancel(thread_id=model_args["thread_id"], run_id=runs.data[0].id)

        await self.async_client.beta.threads.messages.create(thread_id=model_args["thread_id"], role="user", content=message)

        async for chunk in await self.async_client.beta.threads.runs.create(**model_args):
            logger.info(f"chunk received : {chunk}")
            if self.trigger_function_call and chunk.event == "thread.run.step.delta":
                textual_response = False
                if not self.started_streaming:
                    first_chunk_time = time.time()
                    latency = first_chunk_time - start_time
                    logger.info(f"LLM Latency: {latency:.2f} s")
                    self.started_streaming = True
                if not self.gave_out_prefunction_call_message and not textual_response:
                    yield PRE_FUNCTIONAL_CALL_MESSAGE, False, latency
                    self.gave_out_prefunction_call_message = True
                if len(buffer) > 0:
                    yield buffer, False, latency
                    buffer = ''
                yield buffer, False, latency
                buffer = ''
                if chunk.data.delta.step_details.tool_calls[0].function.name and chunk.data.delta.step_details.tool_calls[0].function.arguments is not None:
                    logger.info(f"Should do a function call {chunk.data.delta.step_details.tool_calls[0].function.name}")
                    called_fun = str(chunk.data.delta.step_details.tool_calls[0].function.name)
                    i = [i for i in range(len(tools)) if called_fun == tools[i].function.name][0]
                if (text_chunk := chunk.data.delta.step_details.tool_calls[0].function.arguments):
                    resp += text_chunk
                    logger.info(f"Response from LLM {resp}")
            elif chunk.event == 'thread.message.delta':
                if not self.started_streaming:
                    first_chunk_time = time.time()
                    latency = first_chunk_time - start_time
                    logger.info(f"LLM Latency: {latency:.2f} s")
                    self.started_streaming = True
                textual_response = True
                text_chunk = chunk.data.delta.content[0].text.value
                answer += text_chunk
                buffer += text_chunk
                if len(buffer) >= self.buffer_size and synthesize:
                    buffer_words = buffer.split(" ")
                    text = ' '.join(buffer_words[:-1])

                    if not self.started_streaming:
                        self.started_streaming = True
                    yield text, False, latency
                    buffer = buffer_words[-1]

        if self.trigger_function_call and not textual_response and (all(key in resp for key in tools[i].function.parameters['properties'].keys())) and (called_fun in self.api_params):
            self.gave_out_prefunction_call_message = False
            logger.info(f"Function call parameters {resp}")
            convert_to_request_log(resp, meta_info, self.model, "llm", direction="response", is_cached=False, run_id=self.run_id)
            resp = json.loads(resp)
            func_dict = self.api_params[called_fun]
            logger.info(f"Payload to send {resp} func_dict {func_dict}")

            url = func_dict['url']
            method = func_dict['method']
            param = func_dict['param']
            api_token = func_dict['api_token']
            response = await trigger_api(url=url, method=method.lower(), param=param, api_token=api_token, **resp)
            content = f"We did made a function calling for user. We hit the function : {called_fun}, we hit the url {url} and send a {method} request and it returned us the response as given below: {str(response)} \n\n . Kindly understand the above response and convey this response in a contextual form to user."
            logger.info(f"Logging function call parameters ")
            runs = await self.async_client.beta.threads.runs.list(thread_id=model_args["thread_id"])
            if runs.data and runs.data[0].status in ["in_progress", "queued", "requires_action"]:
                await self.async_client.beta.threads.runs.cancel(thread_id=model_args["thread_id"], run_id=runs.data[0].id)

            await self.async_client.beta.threads.messages.create(thread_id=model_args["thread_id"], role="assistant", content=content)
            async for chunk in await self.async_client.beta.threads.runs.create(**model_args):
                logger.info(f"chunk received : {chunk}")
                if chunk.event == 'thread.message.delta':
                    text_chunk = chunk.data.delta.content[0].text.value
                    answer += text_chunk
                    buffer += text_chunk
                    if len(buffer) >= self.buffer_size and synthesize:
                        buffer_words = buffer.split(" ")
                        text = ' '.join(buffer_words[:-1])

                        if not self.started_streaming:
                            self.started_streaming = True
                        yield text, False, latency
                        buffer = buffer_words[-1]

        if synthesize:  # This is used only in streaming sense
            yield buffer, True, latency
        else:
            yield answer, True, latency
        self.started_streaming = False
