"""Frozen retrieval policy: a saved task never silently adopts a new reranker."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

RERANKER_MODEL_ID = "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1"
RERANKER_REVISION = "1427fd652930e4ba29e8149678df786c240d8825"
POLICY_VERSION = "retrieval-v2"
MAX_PAIR_TOKENS = 512
MAX_QUERY_TOKENS = 128
BATCH_SIZE = 2
RERANKER_FINGERPRINT = (
    f"{RERANKER_MODEL_ID}@{RERANKER_REVISION}:cpu-fp32:pair512:query-head128:"
    "passage-head:batch2:raw-logit:v1"
)


class RetrievalConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_n: int = Field(default=50, ge=5, le=100, strict=True)
    rerank_top_n: int = Field(default=20, ge=1, le=100, strict=True)
    top_k: int = Field(default=5, ge=1, le=10, strict=True)
    reranker: Literal["off", "cross_encoder"] = "off"
    # Unknown historical versions remain parseable. Execution reports degradation
    # instead of silently rewriting an old task's policy or model fingerprint.
    policy_version: str = Field(default=POLICY_VERSION, min_length=1, max_length=100)
    reranker_fingerprint: str = Field(default=RERANKER_FINGERPRINT, min_length=1, max_length=512)

    @model_validator(mode="after")
    def coherent_budgets(self):
        if not self.top_k <= self.rerank_top_n <= 2 * self.candidate_n:
            raise ValueError("Require top_k <= rerank_top_n <= 2*candidate_n")
        return self
