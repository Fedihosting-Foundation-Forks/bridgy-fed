"""Bridgy Fed user-facing app invoked by gunicorn in app.yaml.

Import all modules that define views in the app so that their URL routes get
registered.
"""
from flask_app import app

# import all modules to register their Flask handlers
import activitypub, atproto, follow, pages, redirect, render, superfeedr, webfinger, webmention, xrpc_actor, xrpc_feed, xrpc_graph
