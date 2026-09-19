"""The two-stage map-reduce pipeline.

Stage 0  normalize the RFP into canonical questions       (1 call, or none)
Stage 1  read one vendor document -> VendorSummary        (the "map")
Stage 2  all vendor summaries -> Synthesis                (the "reduce")

No module here imports Streamlit. Progress is reported through a callback the
caller supplies, so the same pipeline runs behind a UI, a script, or a queue.
"""

from .orchestrator import PipelineResult, ProgressEvent, run_pipeline, run_stage2_only

__all__ = ["run_pipeline", "run_stage2_only", "PipelineResult", "ProgressEvent"]
