"""Task15 receipt-admission negative controls, not actual runtime evidence.

Synthetic XML below exercises the verifier only. It never proves a backend ran.
"""
import hashlib
import json
import xml.etree.ElementTree as ET

import pytest

from scripts import verify_demo

pytestmark = pytest.mark.unit


def prerequisite_fixture(root, monkeypatch):
    """Use a small explicit identity set so each corruption is independently tested."""
    state = {'source_digest': 'a' * 64}
    images = {'registry-db': {'reference': 'synthetic', 'image_id': 'sha256:' + 'b' * 64}}
    expected = {name: {('tests.synthetic.' + name, 'test_exact_case')} for name in verify_demo.PREREQUISITES}
    monkeypatch.setattr(verify_demo, 'collected_identities', lambda name: expected[name])
    receipt = {'classification': 'actual', 'source_digest': state['source_digest'], 'images': images, 'suites': []}
    for name in verify_demo.PREREQUISITES:
        document = ET.Element('testsuite', tests='1', failures='0', errors='0', skipped='0')
        ET.SubElement(document, 'testcase', classname='tests.synthetic.' + name, name='test_exact_case')
        path = root / (name + '.xml')
        path.write_bytes(ET.tostring(document))
        receipt['suites'].append({'suite': name, 'junit': path.name, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
    return state, images, receipt


def write_receipt(root, receipt):
    (root / 'prerequisites.json').write_text(json.dumps(receipt))


def test_complete_synthetic_receipt_control_is_admitted(tmp_path, monkeypatch):
    state, images, receipt = prerequisite_fixture(tmp_path, monkeypatch)
    write_receipt(tmp_path, receipt)
    assert verify_demo.validate_prerequisites(tmp_path, state, images) == receipt


@pytest.mark.parametrize('corruption', [
    'absent', 'stale-source', 'stale-image', 'partial', 'duplicate-suite',
    'wrong-case', 'duplicate-case', 'changed-bytes', 'symbolic-link',
])
def test_prerequisite_receipt_rejects_independent_corruptions(tmp_path, monkeypatch, corruption):
    state, images, receipt = prerequisite_fixture(tmp_path, monkeypatch)
    row = receipt['suites'][0]
    path = tmp_path / row['junit']
    if corruption == 'stale-source':
        receipt['source_digest'] = 'c' * 64
    elif corruption == 'stale-image':
        receipt['images']['registry-db']['image_id'] = 'sha256:' + 'd' * 64
        images = {'registry-db': {'reference': 'synthetic', 'image_id': 'sha256:' + 'b' * 64}}
    elif corruption == 'partial':
        receipt['suites'].pop()
    elif corruption == 'duplicate-suite':
        receipt['suites'][-1] = dict(row)
    elif corruption in {'wrong-case', 'duplicate-case'}:
        document = ET.parse(path).getroot()
        if corruption == 'wrong-case':
            document.find('testcase').set('name', 'test_other_case')
        else:
            ET.SubElement(document, 'testcase', document.find('testcase').attrib)
            document.set('tests', '2')
        path.write_bytes(ET.tostring(document))
        row['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
    elif corruption == 'changed-bytes':
        path.write_bytes(path.read_bytes() + b' ')
    elif corruption == 'symbolic-link':
        target = tmp_path / 'other.xml'
        path.rename(target)
        path.symlink_to(target)
    if corruption != 'absent':
        write_receipt(tmp_path, receipt)
    with pytest.raises(ValueError):
        verify_demo.validate_prerequisites(tmp_path, state, images)


@pytest.mark.parametrize('outcome', ['failure', 'error', 'skipped'])
def test_forged_junit_zero_summary_cannot_hide_case_outcome(tmp_path, monkeypatch, outcome):
    state, images, receipt = prerequisite_fixture(tmp_path, monkeypatch)
    row = receipt['suites'][0]
    path = tmp_path / row['junit']
    document = ET.parse(path).getroot()
    ET.SubElement(document.find('testcase'), outcome)
    path.write_bytes(ET.tostring(document))
    row['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
    write_receipt(tmp_path, receipt)
    with pytest.raises(ValueError):
        verify_demo.validate_prerequisites(tmp_path, state, images)


@pytest.mark.parametrize('suite', [name for name in verify_demo.MANDATORY if name not in {'backend_compatibility', 'offline_egress'}])
def test_mandatory_pass_label_and_hash_are_not_runtime_evidence(tmp_path, suite):
    proof = tmp_path / 'proof.txt'
    proof.write_text('PASS')
    records = [{'suite': suite, 'classification': 'actual', 'status': 'PASS', 'tests': 1,
        'evidence_paths': [proof.name], 'evidence_sha256': {proof.name: hashlib.sha256(proof.read_bytes()).hexdigest()}}]
    report = verify_demo.summarize(records, source='a' * 64, versions={}, evidence_root=tmp_path)
    assert report['status'] == 'BLOCKED'
    assert suite in report['missing'], 'A PASS label and existing file must not satisfy this slot'


def runtime_fixture(root):
    """A verifier unit fixture, expressly not a generated actual-stack receipt."""
    source = 'a' * 64
    versions = {'app_image': 'sha256:' + 'b' * 64, 'edge_image': 'sha256:' + 'c' * 64,
        'backend_images': {'registry-db': {'image_id': 'sha256:' + 'd' * 64}}}
    path = root / 'runtime_dependencies.xml'
    document = ET.Element('testsuite', tests='1', failures='0', errors='0', skipped='0')
    ET.SubElement(document, 'testcase', classname='tests.integration.test_operations', name='test_actual_runtime_dependencies')
    path.write_bytes(ET.tostring(document))
    row = {'suite': 'runtime_dependencies', 'classification': 'actual', 'status': 'PASS', 'tests': 1,
        'junit': path.name, 'binding': {'source_digest': source, **versions},
        'evidence_paths': [path.name], 'evidence_sha256': {path.name: hashlib.sha256(path.read_bytes()).hexdigest()}}
    return source, versions, row


def test_complete_synthetic_runtime_slot_control_is_admitted(tmp_path):
    source, versions, row = runtime_fixture(tmp_path)
    report = verify_demo.summarize([row], source=source, versions=versions, evidence_root=tmp_path)
    assert 'runtime_dependencies' not in report['missing']
    assert report['status'] == 'BLOCKED'  # Other mandatory slots remain absent.


@pytest.mark.parametrize('corruption', [
    'source', 'image', 'missing-binding', 'missing-hash', 'changed-bytes', 'wrong-junit',
    'wrong-identity', 'wrong-count', 'duplicate-slot', 'duplicate-case', 'child-failure',
    'child-error', 'child-skipped', 'contract-only', 'no-tests',
])
def test_runtime_slot_rejects_bound_receipt_corruptions(tmp_path, corruption):
    source, versions, row = runtime_fixture(tmp_path)
    rows = [row]
    path = tmp_path / row['junit']
    if corruption == 'source':
        row['binding']['source_digest'] = 'e' * 64
    elif corruption == 'image':
        row['binding']['app_image'] = 'sha256:' + 'f' * 64
    elif corruption == 'missing-binding':
        row.pop('binding')
    elif corruption == 'missing-hash':
        row['evidence_sha256'] = {}
    elif corruption == 'changed-bytes':
        path.write_bytes(path.read_bytes() + b' ')
    elif corruption == 'wrong-junit':
        row['junit'] = 'unrelated.xml'
    elif corruption == 'wrong-count':
        row['tests'] = 2
    elif corruption == 'duplicate-slot':
        rows.append(dict(row))
    elif corruption == 'contract-only':
        row['classification'] = 'contract'
    elif corruption == 'no-tests':
        row['tests'] = None
    else:
        document = ET.parse(path).getroot()
        case = document.find('testcase')
        if corruption == 'wrong-identity':
            case.set('name', 'test_unrelated')
        elif corruption == 'duplicate-case':
            ET.SubElement(document, 'testcase', case.attrib)
            document.set('tests', '2')
            row['tests'] = 2
        else:
            ET.SubElement(case, corruption.removeprefix('child-'))
        path.write_bytes(ET.tostring(document))
        row['evidence_sha256'][path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    report = verify_demo.summarize(rows, source=source, versions=versions, evidence_root=tmp_path)
    assert report['status'] == 'BLOCKED'
    assert 'runtime_dependencies' in report['missing']
