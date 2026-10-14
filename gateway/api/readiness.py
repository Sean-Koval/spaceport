"""Current app-role migration/policy checks and an approved metadata-only probe."""
import asyncio
from datetime import timedelta
from pathlib import Path
from uuid import uuid4
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import select,text
from gateway.registry.models import ProductRow,PolicyRow
from gateway.adapters.base import check_binding
from .models import ReadinessView


class ReadinessService:
    def __init__(self,*,registry,policy,adapter,bindings,bundles,clock,probe_principal_id,probe_product_id,timeout_seconds=3):
        if type(timeout_seconds) not in {int,float} or not 0<timeout_seconds<=5:raise ValueError('Bounded readiness timeout required')
        self.registry,self.policy,self.adapter=registry,policy,adapter
        self.bindings,self.bundles,self.clock=bindings,bundles,clock
        self.probe_principal_id,self.probe_product_id=probe_principal_id,probe_product_id
        self.timeout_seconds=timeout_seconds
        self._lock=asyncio.Lock()

    @staticmethod
    def expected_heads():
        root=Path(__file__).resolve().parents[2]
        config=Config(str(root/'alembic.ini'))
        config.set_main_option('script_location',str(root/'migrations'))
        return ScriptDirectory.from_config(config).get_heads()

    async def check(self):
        if not self.probe_principal_id or not self.probe_product_id:return ReadinessView(ready=False)
        try:
            # Concurrent callers wait only inside the same bounded deadline; no cached permissions.
            async with asyncio.timeout(self.timeout_seconds):
                async with self._lock:
                    return await self._check()
        except Exception:return ReadinessView(ready=False)

    async def _check(self):
        expected=self.expected_heads()
        if len(expected)!=1:return ReadinessView(ready=False)
        deadline=self.clock.now()+timedelta(seconds=self.timeout_seconds)
        async with self.registry.transaction() as uow:
            actual=(await uow.session.execute(text('SELECT version_num FROM public.alembic_version'))).scalars().all()
            if actual!=expected:return ReadinessView(ready=False)
            await uow.session.execute(select(ProductRow.id).limit(1))
            await uow.session.execute(select(PolicyRow.id).limit(1))
        principal=await self.policy.principals.resolve_principal(self.probe_principal_id)
        product=await self.registry.resolve(self.probe_product_id)
        async with self.registry.transaction() as uow:
            decision=await self.policy.authorize(principal,'execute',product,uow)
        binding=await self.bindings.resolve(product.manifest.binding_id,product.manifest.binding_revision)
        check_binding(binding,product.manifest,digest=product.binding_digest)
        endpoint=self.bundles.resolve(product.manifest.model_bundle_digest)
        if binding.context!='consumer' or endpoint.context!='consumer':return ReadinessView(ready=False)
        await self.adapter.validate_binding(product.manifest,decision=decision,deadline=deadline,request_id=str(uuid4()))
        return ReadinessView(ready=True)
