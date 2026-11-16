"""Local HTTP interfaces; request handlers never invoke an Agent graph."""

from incident_desk.api.app import create_app

__all__ = ["create_app"]
