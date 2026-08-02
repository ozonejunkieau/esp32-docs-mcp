"""Wrapper around mlx-embeddings for encoding text into vectors."""

from __future__ import annotations

import mlx.core as mx
from mlx_embeddings import generate, load

# The 4B rather than the 8B of the same family: roughly half the parameters and
# half the resident memory (mcp_server.py keeps the model loaded for the server's
# lifetime), for about a point of MTEB. That trade is better than it looks here --
# the published gap is a multilingual mean, while this corpus is English-only
# technical documentation, and retrieval leans on near-lexical matches like
# "esp_err_t" or "LEDC_CH0_CONF0_REG" where model scale buys least.
#
# Any change here must move EMBEDDING_DIM in schema.py to match (4B is 2560, 8B
# 4096, 0.6B 1024) and requires re-embedding the corpus into a fresh table.
MODEL_NAME = "mlx-community/Qwen3-Embedding-4B-4bit-DWQ"

# Qwen3 embedding models use an instruction prefix for queries but not for
# documents (asymmetric embedding). This measurably improves retrieval.
QUERY_PREFIX = "Instruct: Given a search query, retrieve relevant technical documentation.\nQuery: "


class Embedder:
    """Loads a Qwen3 embedding model once and encodes text into vectors.

    Attributes:
        model_name: Hugging Face repo id of the MLX-format embedding model.
    """

    def __init__(self, model_name: str = MODEL_NAME) -> None:
        """Load the model and tokenizer into memory.

        Args:
            model_name: Hugging Face repo id of the MLX-format embedding model.
        """
        self.model_name = model_name
        self.model, self.tokenizer = load(model_name)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of document chunks (no instruction prefix).

        Args:
            texts: Chunk texts to embed.

        Returns:
            One embedding vector per input text, same order.
        """
        output = generate(self.model, self.tokenizer, texts=texts)
        return output.text_embeds.tolist()

    def embed_query(self, query: str) -> list[float]:
        """Embed a single search query (with instruction prefix).

        Args:
            query: The user's search query text.

        Returns:
            The query embedding vector.
        """
        output = generate(self.model, self.tokenizer, texts=[QUERY_PREFIX + query])
        return output.text_embeds[0].tolist()


if __name__ == "__main__":
    # Quick smoke test: confirm the model loads and produces sane similarity
    # scores before wiring it into the ingest/query pipeline.
    embedder = Embedder()

    docs = [
        "The I2S peripheral supports both master and slave clock modes.",
        "GPIO matrix allows flexible routing of peripheral signals to pins.",
    ]
    doc_vecs = embedder.embed_documents(docs)
    query_vec = embedder.embed_query("How does I2S clock configuration work?")

    print(f"doc embedding dims: {len(doc_vecs[0])}")
    print(f"query embedding dims: {len(query_vec)}")

    sims = mx.matmul(mx.array(query_vec), mx.array(doc_vecs).T)
    print(f"similarities: {sims.tolist()}")