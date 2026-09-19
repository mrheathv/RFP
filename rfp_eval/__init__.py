"""Core pipeline for evaluating vendor responses to an RFP.

Nothing in this package imports Streamlit. The UI layer (``app.py``) depends on
this package, never the other way round, so the pipeline can be driven from a
script, a job queue, or a different frontend without changes.
"""

__version__ = "0.1.0"
