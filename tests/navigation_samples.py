"""Independent game-metadata and deidentified navigation protocol fixtures."""
import json
from pathlib import Path


SAMPLES = Path(__file__).with_name('fixtures') / 'navigation'


def sample_route(code):
    return json.loads((SAMPLES / 'routes.json').read_text())[code]


def sample_protocol(code):
    return json.loads((SAMPLES / (code.lower() + '-protocol.json')).read_text())
