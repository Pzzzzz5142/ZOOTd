"""Explicit local Copilot source; the execution and proof pipeline is shared."""
from __future__ import annotations

import copy
from pathlib import Path

from .prts import PrtsCopilotClient, PrtsError, decode, require
from .util import canonical_json, sha256_bytes


class LocalCopilotClient(PrtsCopilotClient):
    # This is a candidate number in the local namespace, never a PRTS ID.
    CANDIDATE_ID = 1

    def __init__(self, catalog, path: Path, *, snapshot: Path):
        super().__init__(catalog)
        self.path = path.resolve()
        self.raw = self._read()
        self.content = decode(self.raw)
        require(isinstance(self.content, dict), 'Local copilot must be an object.')
        require(isinstance(self.content.get('actions'), list)
                and all(isinstance(a, dict) for a in self.content['actions']),
                'Missing or invalid full copilot actions.')
        self.source = {'kind': 'local', 'path': str(self.path),
                       'sha256': sha256_bytes(self.raw), 'snapshot': str(snapshot)}
        with snapshot.open('xb') as stream:
            stream.write(self.raw)

    def _read(self):
        try:
            with self.path.open('rb') as stream:
                raw = stream.read(self.MAX_BYTES + 1)
        except OSError:
            raise PrtsError('local_source', 'Local copilot cannot be read.') from None
        require(len(raw) <= self.MAX_BYTES, 'Local copilot is too large.')
        return raw

    def _candidate(self, canonical):
        # Reuse the same content/stage/operator validation as the remote source.
        # The envelope is internal only; source.kind always remains local.
        return self._parse({'id': self.CANDIDATE_ID, 'type': 'PRTS',
                            'available': True, 'content': canonical_json(self.content).decode('utf-8')}, canonical)[0]

    def query(self, stage, *, page=1, limit=10):
        canonical = self.catalog.resolve(stage)
        require(type(page) is int and page == 1 and type(limit) is int and 1 <= limit <= 50,
                'Local source has exactly one candidate page.')
        candidate = self._candidate(canonical)
        return {'stage': canonical, 'page': 1, 'has_next': False, 'total': 1,
                'candidates': [candidate.to_dict()], 'source': self.source}

    def get(self, copilot_id, *, stage):
        require(type(copilot_id) is int and copilot_id == self.CANDIDATE_ID,
                'Unknown local candidate.')
        require(self._read() == self.raw, 'Local copilot changed after snapshot.')
        self._candidate(self.catalog.resolve(stage))
        return copy.deepcopy(self.content)

    def _request(self, *args, **kwargs):
        raise PrtsError('local_source', 'Local sources do not make PRTS requests.')
