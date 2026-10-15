"""Private HTTP-backed Domo completion; no serialized result-release authority."""
from dataclasses import dataclass, field
from datetime import datetime

from gateway.source_lifecycle import DomoSendPermit
from .domo_http import DomoHttpClient, DomoProtocolResult, source_error
from .domo_runtime import PreparedDomoQuery


@dataclass(frozen=True)
class DomoCompleted:
    prepared: PreparedDomoQuery
    response: DomoProtocolResult = field(repr=False)
    client: DomoHttpClient = field(repr=False)
    permit: DomoSendPermit = field(repr=False)
    source_watermark: datetime | None

    def evidence(self):
        if (type(self.client) is not DomoHttpClient or type(self.permit) is not DomoSendPermit
            or type(self.prepared) is not PreparedDomoQuery or not self.client.is_closed):
            raise source_error()
        proof=self.client.success_evidence(self.response,self.permit)
        if proof['plan_fingerprint']!=self.prepared.digest:
            raise source_error()
        return proof

    def check_deadline(self):
        self.client.check_deadline(self.permit.context.deadline)
