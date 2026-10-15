"""Hash every loaded v2 asset with the same framing as its Cube entrypoint."""
import hashlib
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]/'cube/reviewed-postgres'


def bundle_digest(model_directory: Path, *, root: Path=ROOT):
    names=sorted(p.name for p in model_directory.iterdir())
    if names!=['bookings.yml']: raise ValueError('Unexpected reviewed model asset')
    files=[(name,root/name) for name in ('cube.js','security.js','security.json')]
    files.extend(('model/'+name,model_directory/name) for name in names)
    value=hashlib.sha256()
    for name,path in files:
        if path.is_symlink() or not path.is_file(): raise ValueError('Invalid reviewed asset file')
        data=path.read_bytes()
        if len(data)>1_048_576: raise ValueError('Oversized reviewed asset')
        value.update((name+'\0'+str(len(data))+'\0').encode());value.update(data)
    return value.hexdigest()


if __name__=='__main__':
    print(json.dumps({name:bundle_digest(ROOT/directory) for name,directory in [('consumer','model'),('fixture_test','test-model')]},sort_keys=True))
