"""Bounded retained product/source catalog identities, entirely offline.

Loading bytes does not prove a source connection or authorize a query. Provider
models and semantic dispatch are fixed imports, never import paths from config.
"""
from dataclasses import dataclass, field
import hashlib
from pathlib import Path
from types import MappingProxyType
from typing import Literal

from pydantic import Field, field_validator, model_validator

from gateway.contracts.canonical import Array, Digest, Identifier, Scalar, StrictModel, canonical_digest
from gateway.contracts.manifest import Manifest, validate_row
from gateway.contracts.query import QueryRequest, validate_query
from gateway.pilot_config import _read_file, _parse_json
from gateway.source_pilot_config import AssetRef


class PackageError(ValueError):
    def __init__(self,code='PACKAGE_INVALID'):
        self.code=code
        super().__init__('The reviewed package or source catalog is unavailable or incompatible.')


class VersionOne(StrictModel):
    schema_version: Literal[1]

    @field_validator('schema_version',mode='before')
    @classmethod
    def exact_version(cls,value):
        if type(value) is not int:
            raise ValueError('Integer schema version required')
        return value


class SourceRef(StrictModel):
    provider: Literal['bigquery','domo']
    source_id: Identifier
    revision: int = Field(ge=1)
    descriptor_digest: Digest
    context: Literal['consumer','fixture_test']


class BindingRef(AssetRef):
    binding_id: Identifier
    revision: int = Field(ge=1)
    content_digest: Digest


class ConsumerCheck(StrictModel):
    request: QueryRequest
    expected_rows: Array[dict[Identifier,Scalar]] = Field(max_length=1000)


class ProductPackage(VersionOne):
    package_id: Identifier
    revision: int = Field(ge=1)
    manifest: Manifest = Field(repr=False)
    consumer_binding_ref: BindingRef
    fixture_binding_ref: BindingRef
    consumer_source_ref: SourceRef
    fixture_source_ref: SourceRef
    policy_seed_ref: AssetRef
    fixture_catalog_ref: AssetRef
    bundle_catalog_ref: AssetRef
    consumer_check: ConsumerCheck
    fixture_equivalence_ref: AssetRef

    @model_validator(mode='after')
    def coherent_package(self):
        consumer,fixture=self.consumer_source_ref,self.fixture_source_ref
        if (consumer.provider!=fixture.provider or consumer.context!='consumer' or fixture.context!='fixture_test'
            or consumer.source_id==fixture.source_id
            or self.consumer_binding_ref.binding_id==self.fixture_binding_ref.binding_id
            or (self.manifest.binding_id,self.manifest.binding_revision)
                != (self.consumer_binding_ref.binding_id,self.consumer_binding_ref.revision)):
            raise ValueError('Independent exact consumer and fixture identities required')
        request=self.consumer_check.request
        if (request.product_id,request.version)!=(self.manifest.product_id,self.manifest.version) or request.idempotency_key is not None:
            raise ValueError('Consumer check requires this exact published version')
        validate_query(self.manifest,request)
        for row in self.consumer_check.expected_rows:validate_row(self.manifest.outputs,row)
        return self

    @property
    def digest(self):
        return canonical_digest(self)


class PackageIndex(VersionOne):
    packages: Array[AssetRef] = Field(min_length=1,max_length=25)

    @model_validator(mode='after')
    def distinct(self):
        if len({r.path for r in self.packages})!=len(self.packages) or len({r.sha256 for r in self.packages})!=len(self.packages):
            raise ValueError('Package index contains aliases')
        return self


class SourceIndex(VersionOne):
    sources: Array[AssetRef] = Field(min_length=2,max_length=50)

    @model_validator(mode='after')
    def distinct(self):
        if len({r.path for r in self.sources})!=len(self.sources) or len({r.sha256 for r in self.sources})!=len(self.sources):
            raise ValueError('Source index contains aliases')
        return self


def read_asset(root,reference):
    try:
        ref=AssetRef.model_validate(reference.model_dump(include={'path','sha256'}) if isinstance(reference,AssetRef) else reference)
        root=Path(root)
        if not root.is_absolute() or '..' in root.parts:
            raise ValueError
        raw=_read_file(str(root/ref.path),1_048_576)
        if hashlib.sha256(raw).hexdigest()!=ref.sha256:
            raise ValueError
        return _parse_json(raw)
    except (ValueError,TypeError,OSError,RecursionError):
        raise PackageError() from None


def _source_model(provider):
    try:
        if provider=='bigquery':
            from gateway.adapters.bigquery_runtime import BigQuerySource
            return BigQuerySource
        if provider=='domo':
            from gateway.adapters.domo_runtime import DomoSource
            return DomoSource
    except ImportError:
        raise PackageError('SOURCE_RUNTIME_UNAVAILABLE') from None
    raise PackageError()


class SourceCatalog:
    def __init__(self,sources):
        entries={}
        for source in sources:
            key=(source.source_id,source.revision)
            if key in entries:
                raise PackageError()
            entries[key]=source
        if not 2<=len(entries)<=50:
            raise PackageError()
        self._entries=MappingProxyType(entries)

    @classmethod
    def load(cls,root,reference):
        try:
            index=SourceIndex.model_validate(read_asset(root,reference))
            sources=[]
            for ref in index.sources:
                raw=read_asset(root,ref)
                if not isinstance(raw,dict):
                    raise PackageError()
                sources.append(_source_model(raw.get('provider')).model_validate(raw))
            return cls(sources)
        except PackageError:
            raise
        except (ValueError,TypeError,AttributeError):
            raise PackageError() from None

    def resolve(self,reference):
        try:
            ref=SourceRef.model_validate(reference)
            source=self._entries[(ref.source_id,ref.revision)]
            if (source.provider,source.context,source.digest)!=(ref.provider,ref.context,ref.descriptor_digest):
                raise ValueError
            return source
        except (ValueError,KeyError,TypeError,AttributeError):
            raise PackageError() from None

    @property
    def sources(self):
        return tuple(self._entries.values())


@dataclass(frozen=True)
class RetainedPackage:
    """Hashed bytes only. Runtime MUST additionally validate adapter semantics."""
    package: ProductPackage
    consumer_source: object = field(repr=False)
    fixture_source: object = field(repr=False)
    assets: object = field(repr=False)


class ProductCatalog:
    def __init__(self,packages):
        rows={}
        product_versions=set()
        for retained in packages:
            p=retained.package
            key=(p.package_id,p.revision)
            product=(p.manifest.product_id,p.manifest.version)
            if key in rows or product in product_versions:
                raise PackageError()
            rows[key]=retained;product_versions.add(product)
        if not 1<=len(rows)<=25:
            raise PackageError()
        self._entries=MappingProxyType(rows)

    @classmethod
    def load(cls,root,reference,sources):
        try:
            index=PackageIndex.model_validate(read_asset(root,reference))
            retained=[]
            for ref in index.packages:
                p=ProductPackage.model_validate(read_asset(root,ref))
                consumer=sources.resolve(p.consumer_source_ref)
                fixture=sources.resolve(p.fixture_source_ref)
                assets={name:read_asset(root,getattr(p,name)) for name in (
                    'consumer_binding_ref','fixture_binding_ref','policy_seed_ref','fixture_catalog_ref',
                    'bundle_catalog_ref','fixture_equivalence_ref')}
                for name in ('consumer_binding_ref','fixture_binding_ref'):
                    binding=assets[name];expected=getattr(p,name)
                    if (not isinstance(binding,dict) or (binding.get('binding_id'),binding.get('revision'))
                        !=(expected.binding_id,expected.revision) or canonical_digest(binding)!=expected.content_digest):
                        raise PackageError()
                # Canonical immutability of nested assets prevents caller mutation.
                from gateway.contracts.canonical import freeze
                retained.append(RetainedPackage(p,consumer,fixture,freeze(assets)))
            return cls(retained)
        except PackageError:
            raise
        except (ValueError,TypeError,KeyError,AttributeError):
            raise PackageError() from None

    @property
    def packages(self):
        return tuple(self._entries.values())


def build_source_check_plan(package):
    """Derivative receipt inputs avoid a package-to-plan content hash cycle."""
    from scripts.spaceport import SourceCheckPlan
    from gateway.contracts.execution import Provenance
    p=ProductPackage.model_validate(package.model_dump(mode='python'))
    manifest=p.manifest
    return SourceCheckPlan(request=validate_query(manifest,p.consumer_check.request),
        configured_package_digest=p.digest,configured_source_id=p.consumer_source_ref.source_id,
        configured_source_digest=p.consumer_source_ref.descriptor_digest,
        expected_product_digest=manifest.content_digest,
        expected_provenance=Provenance(binding_id=manifest.binding_id,binding_revision=manifest.binding_revision,
            adapter_revision=manifest.adapter_revision,model_bundle_digest=manifest.model_bundle_digest,links=()),
        expected_rows=p.consumer_check.expected_rows)
