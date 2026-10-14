#!/usr/bin/env python3
"""Scoped observer admission support; privileged observation is not approved.

Business workflows run without elevation. Packet-level acceptance remains
BLOCKED until an independently reviewed, explicitly approved observer exists.
"""
import argparse
import errno
import json
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
import time
import re
import ipaddress
from scripts.demo_runtime import load_state,require_docker,inspect_stack,verify_images,private_json

DENIED={errno.ENETUNREACH,errno.EHOSTUNREACH,errno.EACCES,errno.EPERM,errno.ENETDOWN}


def denied_socket(record):
    return record.get('outcome')=='error' and type(record.get('errno')) is int and record['errno'] in DENIED


def counter_snapshot():
    text=Path('/proc/self/net/snmp').read_text().splitlines()
    result={}
    for head,values in zip(text[::2],text[1::2]):
        names=head.split();numbers=values.split()
        if names[0]=='Ip:':
            row=dict(zip(names[1:],numbers[1:]))
            result={name:int(row[name]) for name in ('OutNoRoutes','OutDiscards') if name in row}
    return result


def socket_attempt(family,kind,address):
    sock=socket.socket(family,kind);sock.settimeout(1.5)
    try:
        sock.connect(address)
        if kind==socket.SOCK_DGRAM:
            sock.send(b'dpg-synthetic-egress-probe');sock.recv(32)
        return {'outcome':'connected'}
    except TimeoutError:return {'outcome':'timeout'}
    except OSError as error:return {'outcome':'error','errno':error.errno}
    finally:sock.close()


def resolver_attempt():
    # Ask the actual container's Docker embedded resolver, not the host's
    # /etc/resolv.conf. No private hostname or identity leaves this test.
    query=struct.pack('!HHHHHH',0x4757,0x0100,1,0,0,0)+b'\x07example\x03com\0'+struct.pack('!HH',1,1)
    sock=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);sock.settimeout(2)
    try:
        sock.sendto(query,('127.0.0.11',53));data,_=sock.recvfrom(4096)
        if len(data)<12:return {'outcome':'invalid'}
        identity,flags,questions,answers,authority,additional=struct.unpack('!HHHHHH',data[:12])
        return {'outcome':'dns','rcode':flags&15,'answers':answers,'matching':identity==0x4757}
    except TimeoutError:return {'outcome':'timeout'}
    except OSError as error:return {'outcome':'error','errno':error.errno}
    finally:sock.close()


def namespace_probe():
    before=counter_snapshot()
    probes={
        'tcp_ipv4':socket_attempt(socket.AF_INET,socket.SOCK_STREAM,('1.1.1.1',443)),
        'udp_ipv4':socket_attempt(socket.AF_INET,socket.SOCK_DGRAM,('8.8.8.8',53)),
        'tcp_ipv6':socket_attempt(socket.AF_INET6,socket.SOCK_STREAM,('2606:4700:4700::1111',443)),
        'udp_ipv6':socket_attempt(socket.AF_INET6,socket.SOCK_DGRAM,('2001:4860:4860::8888',53)),
        'embedded_dns':resolver_attempt()}
    after=counter_snapshot()
    dns=probes['embedded_dns']
    dns_denied=(dns.get('outcome')=='dns' and dns.get('matching') is True and dns.get('answers')==0 and dns.get('rcode') in {2,5}) or denied_socket(dns)
    # A DNS timeout alone does not prove the daemon did not forward externally.
    passed=all(denied_socket(probes[name]) for name in ('tcp_ipv4','udp_ipv4','tcp_ipv6','udp_ipv6')) and dns_denied
    return {'status':'PASS' if passed else 'BLOCKED','probes':probes,'kernel_before':before,'kernel_after':after,
        'kernel_delta':{key:after[key]-before.get(key,0) for key in after},
        'routes':Path('/proc/self/net/route').read_text().splitlines()}


WORKFLOW_SUITES={'publisher_browser','mcp_clients','runtime_dependencies','readiness','consumer_rest'}
MAX_CAPTURE_BYTES=8*1024*1024


def validate_resolver(text,configured_dns):
    """Tag-reviewed Moby28.0.4 override semantics; no host resolver assumption."""
    if not isinstance(text,str) or len(text.encode())>16384:raise ValueError('Unbounded resolver metadata')
    lines=[line.strip() for line in text.splitlines()]
    servers=[line.split()[1:] for line in lines if line.startswith('nameserver ')]
    if configured_dns!=['127.0.0.1'] or servers!=[['127.0.0.11']]:raise ValueError('Unsupported resolver configuration')
    if [line for line in lines if line.startswith('# ExtServers:')]!=['# ExtServers: [127.0.0.1]']:
        raise ValueError('Resolver forwarding is not confined to the container namespace')
    if [line for line in lines if line.startswith('# Overrides:')]!=['# Overrides: [nameservers]']:
        raise ValueError('Explicit DNS namespace override is unverified')
    if any('host(' in line or 'default nameserver' in line.lower() or 'Invalid nameservers:' in line for line in lines):
        raise ValueError('Fallback or host resolver forwarding is unsupported')
    return {'mechanism':'moby-28.0.4-explicit-dns-namespace-override','upstream':'127.0.0.1','namespace':'container'}


def packet_endpoint(value):
    try:return str(ipaddress.ip_address(value)),None
    except ValueError:
        host,port=value.rsplit('.',1)
        if not port.isdecimal() or not 0<int(port)<=65535:raise ValueError('Invalid packet port')
        return str(ipaddress.ip_address(host)),int(port)


def matches_control(packet,control):
    for name in ('source','source_port','target','target_port'):
        if name in control and packet[name]!=control[name]:return False
    if 'qname' in control:
        if not re.fullmatch(r'dpg-[a-f0-9]{24}\.invalid',control['qname']):raise ValueError('Invalid DNS control identity')
        return 'A? '+control['qname']+'. ' in packet['detail']
    return 'UDP, length 32' in packet['detail']


def validate_capture(text,allowed_addresses,*,controls=None,scope='namespace',runtime_addresses=None):
    """Evaluate scoped Linux packet text; controls prove observed queue barriers.

    ps_recv is diagnostic: Linux counts duplicate loopback delivery differently
    from libpcap. End sentinels observed after the workflow, zero capture loss,
    and unchanged interface loss counters establish the required drain boundary.
    """
    if not controls or len(controls)<2:raise ValueError('Observed begin/end positive controls are required')
    if scope not in {'namespace','bridge'} or (scope=='bridge' and runtime_addresses is None):raise ValueError('Unattributable capture scope')
    if len(text.encode())>MAX_CAPTURE_BYTES or 'listening on' not in text:raise ValueError('Missing bounded capture start')
    counts={};packets=[];excluded=0
    for line in text.splitlines():
        match=re.fullmatch(r'(\d+) packets? (captured|received by filter|dropped by kernel|dropped by interface)',line)
        if match:
            if match[2] in counts:raise ValueError('Duplicate capture statistics')
            counts[match[2]]=int(match[1]);continue
        if line.startswith(('tcpdump:','listening on ','dropped privs to ')):continue
        match=re.search(r'\b(IP6?) (\S+) > (\S+): (.*)',line)
        if not match:raise ValueError('Unknown or incomplete packet evidence')
        source,source_port=packet_endpoint(match[2]);target,target_port=packet_endpoint(match[3])
        packet={'source':source,'source_port':source_port,'target':target,'target_port':target_port,'detail':match[4]}
        packets.append(packet)
        control_packet=any(matches_control(packet,control) for control in controls)
        if scope=='bridge' and source not in runtime_addresses and target not in runtime_addresses and not control_packet:
            excluded+=1;continue
        for address in (source,target):
            if not ipaddress.ip_address(address).is_loopback and address not in allowed_addresses:raise ValueError('Forbidden attributed traffic observed')
    if not {'captured','received by filter','dropped by kernel'}<=set(counts) or any(counts.get(key,0) for key in ('dropped by kernel','dropped by interface')):
        raise ValueError('Incomplete or lost capture')
    if counts['captured']!=len(packets):raise ValueError('Printed capture is incomplete')
    positions=[]
    for control in controls:
        found=[index for index,packet in enumerate(packets) if matches_control(packet,control)]
        if not found:raise ValueError('Required positive control is absent')
        positions.append(found[0])
    if positions[0]>=positions[-1]:raise ValueError('Ending drain control precedes beginning control')
    return {**counts,'excluded_unrelated':excluded,'controls_observed':len(controls)}


# No privileged observer is installed, invoked or approved by this source.
# Adding an implementation/profile requires the separately scoped approval and
# independent review described in docs/operations/observer-proposal.md.
APPROVED_OBSERVER_PROFILES=frozenset()


def validate_report(report,evidence_root):
    if report.get('status')!='PASS' or report.get('classification')!='actual':raise ValueError('No successful packet observation')
    if report.get('observer_profile') not in APPROVED_OBSERVER_PROFILES:
        raise ValueError('Packet observer installation/invocation is not approved')
    # Intentionally no producer/profile can reach acceptance in this revision.
    # Parser support alone is not permission or proof that packets were observed.
    raise ValueError('No reviewed packet observer implementation is available')


def observer_preflight():
    return {'status':'BLOCKED','reason':'approved_scoped_observer_not_available','privileged_action_performed':False}


def verify(state,evidence,*,workflow=None):
    if workflow is None:raise ValueError('Successful workflow coverage is required')
    require_docker();verify_images(state);records=inspect_stack(state)
    evidence.mkdir(mode=0o700,parents=False,exist_ok=False)
    preflight=observer_preflight()
    began=time.time_ns();workflow_records=workflow();ended=time.time_ns()
    final=inspect_stack(state)
    if final!=records:raise RuntimeError('Runtime changed during business workflow')
    covered={row['suite'] for row in workflow_records if row.get('status')=='PASS' and row.get('classification')=='actual' and row.get('tests',0)>0}
    report={'suite':'offline_egress','classification':'actual','status':'BLOCKED',
        'reason':preflight['reason'],'packet_observation_performed':False,'observer_preflight':preflight,
        'project':state['project'],'source_digest':state['source_digest'],'observed_records':records,
        'workflow_start_ns':began,'workflow_end_ns':ended,'successful_business_suites':sorted(covered),
        'scope':'Exact Docker topology inspected; no packet-level egress assertion. Business tests ran without privilege elevation.'}
    private_json(evidence/'network-report.json',report)
    return report


def main():
    parser=argparse.ArgumentParser(description='Observe denied synthetic-runtime egress without host mutation')
    parser.add_argument('--state-dir',type=Path)
    parser.add_argument('--evidence-dir',type=Path)
    parser.add_argument('--namespace-probe',action='store_true')
    args=parser.parse_args()
    try:
        if args.namespace_probe:
            print(json.dumps(namespace_probe()));return 0
        if args.state_dir is None or args.evidence_dir is None:raise ValueError('Explicit state and evidence directories required')
        report=verify(load_state(args.state_dir),args.evidence_dir)
    except Exception:
        report={'suite':'offline_egress','classification':'actual','status':'BLOCKED',
            'reason':'Runtime denial or complete read-only network observation could not be established.'}
    print(json.dumps(report,sort_keys=True));return 0 if report['status']=='PASS' else 1


if __name__=='__main__':raise SystemExit(main())
