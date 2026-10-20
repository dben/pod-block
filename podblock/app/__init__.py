"""The app: what pod-block does beyond the detector, short of HTTP.

Following feeds, the job queue and the worker that runs it, cutting,
private feeds, users and clean-up. The web app (`podblock.web`) and the
worker process both run on it. It never imports the web app, nor anything
made for HTTP; `tests/test_layers.py` checks that.
"""
