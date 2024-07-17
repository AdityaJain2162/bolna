import os
import time
import asyncio
from typing import List, Tuple, Generator, AsyncGenerator
import dotenv

from llama_index.core import VectorStoreIndex, StorageContext
from llama_index.core.llms import ChatMessage
from llama_index.core.tools import QueryEngineTool, ToolMetadata
from llama_index.llms.openai import OpenAI
from llama_index.vector_stores.lancedb import LanceDBVectorStore
from llama_index.agent.openai import OpenAIAgent

from .base_agent import BaseAgent
from bolna.helpers.logger_config import configure_logger
from bolna.helpers.data_ingestion_pipe import lance_db
import requests
import aiohttp
from llama_index.core.tools import FunctionTool
dotenv.load_dotenv()
logger = configure_logger(__name__)



class LlamaIndexAttachRag(BaseAgent):
    """
    A class that implements a RAG (Retrieval-Augmented Generation) system using LlamaIndex.

    This class sets up and manages an OpenAI-based language model, a vector store, and an agent
    for performing document search and question answering tasks.

    Attributes:
        vector_id (str): Identifier for the vector store.
        temperature (float): Temperature setting for the language model.
        model (str): The name of the OpenAI model to use.
        buffer (int): Size of the token buffer for streaming responses.
        max_tokens (int): Maximum number of tokens for the language model output.
        OPENAI_KEY (str): OpenAI API key retrieved from environment variables.
        llm (OpenAI): Instance of the OpenAI language model.
        vector_store (LanceDBVectorStore): Vector store for document storage and retrieval.
        vector_index (VectorStoreIndex): Index built on top of the vector store.
        query_engine: Query engine for searching the vector index.
        agent (OpenAIAgent): Agent that uses the query engine to answer questions.
    """

    def __init__(self, rag_id:str ,vector_id: str, temperature: float, model: str, buffer: int = 40, max_tokens: int = 100):
        """
        Initialize the LlamaIndexRag instance.

        Args:
            vector_id (str): Identifier for the vector store.
            temperature (float): Temperature setting for the language model.
            model (str): The name of the OpenAI model to use.
            buffer (int, optional): Size of the token buffer for streaming responses. Defaults to 40.
            max_tokens (int, optional): Maximum number of tokens for the language model output. Defaults to 100.
        """
        super().__init__()
        self.vector_id = vector_id
        self.temperature = temperature
        self.model = model
        self.buffer = buffer
        self.max_tokens = max_tokens
        # self.id_rag = "index-a6b1e962-19fe-4b12-9ca5-f1f7e76d12c2"
        # self.index = "29212322-fb8d-49fe-9baa-90598b311f67"
        self.id_rag = rag_id
        self.url = f'http://127.0.0.1:8000/rag-retrive/{self.id_rag}/{self.vector_id}'
        self.OPENAI_KEY = os.getenv('OPENAI_API_KEY')

        self._setup_llm()
        self._setup_vector_store()
        self._setup_agent()
    
    

    def _setup_llm(self):
        """Set up the OpenAI language model."""
        self.llm = OpenAI(
            model=self.model,
            temperature=self.temperature,
            api_key=self.OPENAI_KEY,
            max_tokens=self.max_tokens
        )
    def _retrive_function(self,query):
        "this frunction grap extra imformation from the knowledge base"
        headers = {
            'accept': 'application/json',
        }
        params = {
            'query': query,
        }
        response = requests.get(
            self.url,
            params=params,
            headers=headers,
        )
        return response.text
    def _setup_vector_store(self):
        """Set up the vector store and index."""
        logger.info(f"LLAMA INDEX VALUES: {(lance_db, self.vector_id)}")
        self.vector_store = LanceDBVectorStore(uri=lance_db, table_name=self.vector_id)
        storage_context = StorageContext.from_defaults(vector_store=self.vector_store)
        self.vector_index = VectorStoreIndex([], storage_context=storage_context)
        self.query_engine = self.vector_index.as_query_engine()

    def _setup_agent(self):
        """Set up the OpenAI agent with the query engine tool."""
        tool = FunctionTool.from_defaults(fn=self._retrive_function)
        self.agent = OpenAIAgent.from_tools(tools=[tool], verbose=True)
        logger.info("LLAMA INDEX AGENT IS CREATED")

    @staticmethod
    def convert_history(history: List[dict]) -> Tuple[ChatMessage, List[ChatMessage]]:
        """
        Convert a list of message dictionaries to ChatMessage objects.

        Args:
            history (List[dict]): A list of dictionaries containing message data.

        Returns:
            Tuple[ChatMessage, List[ChatMessage]]: A tuple containing the latest message and the chat history.
        """
        message = ChatMessage(role=history[-1]['role'], content=history[-1]['content'])
        chat_history = [ChatMessage(role=msg['role'], content=msg['content']) for msg in history[:-1]]
        return message, chat_history

    async def generate(self, message: List[dict], **kwargs) -> AsyncGenerator[Tuple[str, bool, float, bool], None]:
        """
        Generate a response based on the input message and chat history.

        This method streams the generated response, yielding chunks of text along with metadata.

        Args:
            message (List[dict]): A list of dictionaries containing the message data and chat history.
            **kwargs: Additional keyword arguments (unused in this implementation).

        Yields:
            Tuple[str, bool, float, bool]: A tuple containing:
                - The generated text chunk.
                - A boolean indicating if this is the final chunk.
                - The latency of the first token.
                - A boolean indicating if the response was truncated (always False in this implementation).
        """
        logger.info(f"Generate function Input: {message}")
        message, history = await asyncio.to_thread(self.convert_history, message)
        
        buffer = ""
        latency = -1
        start_time = time.time()

        token_generator = await asyncio.to_thread(self.agent.stream_chat, message.content, history)
        
        for token in token_generator.response_gen:
            if latency < 0:
                latency = time.time() - start_time
            buffer += " " + token
            if len(buffer.split()) >= self.buffer:
                yield buffer.strip(), False, latency, False
                logger.info(f"LLM BUFFER FULL BUFFER OUTPUT: {buffer}")
                buffer = ""

        if buffer:
            logger.info(f"LLM BUFFER FLUSH BUFFER OUTPUT: {buffer}")
            yield buffer.strip(), True, latency, False