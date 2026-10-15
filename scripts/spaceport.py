"""Supported private command entrypoint: python -m scripts.spaceport --help.

Every network command is explicit. Product test executes isolated fixtures;
source check executes an ordinary already-published consumer request. No command
creates credentials, grants, cloud infrastructure or a source activation flag.
"""
import argparse
from collections import Counter
import json
from typing import Literal

from pydantic import Field
from gateway.contracts.canonical import Array, Digest, Identifier, Scalar, StrictModel, canonical_bytes, canonical_digest
from gateway.contracts.execution import ResultEnvelope, Provenance
from gateway.contracts.query import QueryRequest
from scripts.spaceport_client import CLIError, ClientConfig, GatewayClient, ReviewReceipt, VersionedFile
from scripts.spaceport_files import check_output, read_bytes, read_json, write_json


class SourceCheckPlan(VersionedFile):
    schema_version: Literal[1] = 1
    request: QueryRequest
    configured_package_digest: Digest
    configured_source_id: Identifier
    configured_source_digest: Digest
    expected_product_digest: Digest
    expected_provenance: Provenance
    expected_rows: Array[dict[Identifier, Scalar]] = Field(max_length=1000)


class Parser(argparse.ArgumentParser):
    def error(self, _message):
        raise CLIError('INVALID_COMMAND')


def parser():
    root = Parser(description='Explicit private Spaceport product workflow')
    groups = root.add_subparsers(dest='group', required=True)
    product = groups.add_parser('product').add_subparsers(dest='action',required=True)
    source = groups.add_parser('source').add_subparsers(dest='action',required=True)
    pilot=groups.add_parser('pilot').add_subparsers(dest='action',required=True)
    for name in ('init','build','validate','render','plan'):
        p=pilot.add_parser(name)
        if name=='init':p.add_argument('--provider',required=True,choices=('bigquery','domo'))
        if name=='build':p.add_argument('--input-file',required=True)
        if name in {'validate','render','plan'}:p.add_argument('--config',required=True)
        if name in {'init','build','render'}:p.add_argument('--directory',required=True)
        if name=='render':p.add_argument('--deployment-file',required=True)
        if name=='plan':
            p.add_argument('--package-id',required=True);p.add_argument('--revision',type=int,required=True);p.add_argument('--plan-file',required=True)
    commands = {}
    for name in ('save','test','preview','review','publish','query','result'):
        commands[name] = product.add_parser(name)
    commands['check'] = source.add_parser('check')
    for p in commands.values():
        p.add_argument('--client-config',required=True)
        p.add_argument('--gateway')
        p.add_argument('--token-file',required=True)
        p.add_argument('--receipt-file',required=True)
    for name in ('test','preview','review','publish'):
        commands[name].add_argument('--draft-id',required=True)
    commands['save'].add_argument('--manifest',required=True)
    commands['save'].add_argument('--expected-revision',type=int,required=True)
    commands['save'].add_argument('--draft-id')
    commands['preview'].add_argument('--test-run-id',required=True)
    commands['publish'].add_argument('--review-receipt',required=True)
    commands['publish'].add_argument('--reason-file',required=True)
    commands['query'].add_argument('--request-file',required=True)
    commands['check'].add_argument('--plan-file',required=True)
    for name in ('query','check'):
        commands[name].add_argument('--operation-file',required=True)
        commands[name].add_argument('--result-file')
        commands[name].add_argument('--retry-ambiguous',action='store_true')
    commands['result'].add_argument('--execution-id',required=True)
    commands['result'].add_argument('--cursor')
    commands['result'].add_argument('--result-file',required=True)
    return root


def _envelope_identity(result):
    value = result.model_dump(mode='json',exclude={'rows','next_cursor','freshness','warnings'})
    # ResultService reassesses age/status/warnings on every authorized read.
    value['freshness_max_age_seconds'] = result.freshness.max_age_seconds if result.freshness else None
    return canonical_bytes(value)


def _complete_result(client, execution_id, request, *, initial=None):
    """Bounded complete pagination. No silent truncation or changed result identity."""
    current = initial if initial is not None else client.result(execution_id)
    if current.state != 'succeeded':
        return current
    if not isinstance(current,ResultEnvelope):
        raise CLIError()
    if current.product_id != request.product_id or current.version != request.version:
        raise CLIError()
    first = current
    identity = _envelope_identity(first)
    rows = list(first.rows)
    seen = set()
    for _ in range(first.row_count + 1):
        client.remaining()
        if not current.next_cursor:
            if len(rows) != first.row_count:
                raise CLIError()
            try:
                return ResultEnvelope.model_validate({**current.model_dump(mode='json'),'rows':rows,'next_cursor':None})
            except ValueError:
                raise CLIError() from None
        if not current.rows or current.next_cursor in seen:
            raise CLIError()
        seen.add(current.next_cursor)
        current = client.result(execution_id,cursor=current.next_cursor)
        if not isinstance(current,ResultEnvelope) or current.state != 'succeeded' or _envelope_identity(current) != identity:
            raise CLIError()
        rows.extend(current.rows)
        if len(rows)>1000 or len(canonical_bytes(rows))>1048576:
            raise CLIError()
    raise CLIError()


def run(args):
    if args.group=='pilot':
        from scripts.spaceport_setup import run as setup_run
        return setup_run(args)
    config = ClientConfig.from_file(args.client_config)
    check_output(args.receipt_file)
    if getattr(args,'result_file',None):
        check_output(args.result_file)
    mutation = args.action in {'save','test','publish','query','check'}
    dispatched = False
    try:
        with GatewayClient(config,token_file=args.token_file,gateway=args.gateway) as client:
            base = dict(schema_version=1,gateway_origin=client.origin,operation=args.group+'-'+args.action)
            if args.action=='save':
                manifest = read_json(args.manifest)
                dispatched = True
                value = client.save(manifest,expected_revision=args.expected_revision,draft_id=args.draft_id)
                receipt = {**base,'outcome':value.model_dump(mode='json'),'draft_url':client.origin+'/drafts/'+value.id+'/edit'}
            elif args.action=='test':
                dispatched = True
                value = client.test(args.draft_id)
                receipt = {**base,'data_context':'isolated-fixture','outcome':value.model_dump(mode='json')}
            elif args.action=='preview':
                value = client.preview(args.draft_id,args.test_run_id)
                receipt = {**base,'data_context':'isolated-fixture','outcome':value.model_dump(mode='json')}
            elif args.action=='review':
                value = client.review(args.draft_id)
                receipt = ReviewReceipt(gateway_origin=client.origin,review=value).model_dump(mode='json')
            elif args.action=='publish':
                review = ReviewReceipt.model_validate(read_json(args.review_receipt,private=True))
                reason = read_bytes(args.reason_file,limit=4096).decode('utf-8').strip()
                dispatched = True
                value = client.publish(args.draft_id,review=review,reason=reason)
                receipt = {**base,'outcome':value.model_dump(mode='json')}
            elif args.action=='result':
                value = client.result(args.execution_id,cursor=args.cursor)
                write_json(args.result_file,value.model_dump(mode='json'))
                receipt = {**base,'execution_id':value.execution_id,'state':value.state,
                    'has_next_page':isinstance(value,ResultEnvelope) and value.next_cursor is not None}
            else:
                plan = SourceCheckPlan.model_validate(read_json(args.plan_file)) if args.action=='check' else None
                request = plan.request if plan else QueryRequest.model_validate(read_json(args.request_file))
                if plan:
                    description = client.describe(request.product_id,request.version)
                    if (description.digest != plan.expected_product_digest
                        or canonical_bytes(description.provenance) != canonical_bytes(plan.expected_provenance)):
                        raise CLIError('SOURCE_PLAN_DRIFT')
                dispatched = True
                value = client.query(request.model_dump(mode='json'),operation_file=args.operation_file,
                    context_digest=canonical_digest(plan) if plan else None,retry_ambiguous=args.retry_ambiguous)
                if value.state=='succeeded':
                    value = _complete_result(client,value.execution_id,request,
                        initial=value if isinstance(value,ResultEnvelope) else None)
                result = value if isinstance(value,ResultEnvelope) and value.state=='succeeded' else None
                receipt = {**base,'data_context':'consumer-source','execution_id':value.execution_id,'state':value.state}
                if plan:
                    if result is not None and (result.product_digest != plan.expected_product_digest
                        or canonical_bytes(result.provenance) != canonical_bytes(plan.expected_provenance)):
                        raise CLIError('SOURCE_PLAN_DRIFT')
                    receipt['configured'] = {'source_id':plan.configured_source_id,'source_digest':plan.configured_source_digest,
                        'package_digest':plan.configured_package_digest}
                    receipt['observed'] = None if result is None else dict(
                        product_id=result.product_id,version=result.version,product_digest=result.product_digest,
                        provenance=result.provenance.model_dump(mode='json'),
                        known_answer_passed=Counter(map(canonical_bytes,result.rows))==Counter(map(canonical_bytes,plan.expected_rows)))
                if result is not None and args.result_file:
                    write_json(args.result_file,result.model_dump(mode='json'))
            client.remaining()
            failed = (getattr(value,'status',None)=='failed' or getattr(value,'state',None) in {'failed','timed_out','expired'}
                or (args.action=='check' and receipt.get('observed') is not None
                    and receipt['observed']['known_answer_passed'] is False))
            pending = getattr(value,'state',None) in {'accepted','running'}
            status = 'failed' if failed else ('pending' if pending else ('passed' if args.action=='check' else 'recorded'))
            if args.action!='review':receipt['outcome_status'] = status
            write_json(args.receipt_file,receipt)
            print(json.dumps({'status':status,'operation':args.group+'-'+args.action},sort_keys=True))
            return 1 if failed else (4 if pending else 0)
    except CLIError:
        raise
    except (ValueError,TypeError,OSError,UnicodeError,RecursionError):
        raise CLIError(uncertain=mutation and dispatched) from None


def main(argv=None):
    try:
        return run(parser().parse_args(argv))
    except CLIError as error:
        print(json.dumps({'status':'uncertain' if error.uncertain else 'unavailable','code':error.code},sort_keys=True))
        return 3 if error.uncertain else 2
    except (ValueError,TypeError,OSError,UnicodeError,RecursionError):
        print(json.dumps({'status':'unavailable','code':'INVALID_OPERATOR_INPUT'},sort_keys=True))
        return 2


if __name__=='__main__':
    raise SystemExit(main())
