import base64, csv, hashlib, io
from zipfile import ZipFile
from fastws.build_backend import build_editable


def test_editable_wheel(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path/'pyproject.toml').write_text(r'''[tool.maturin]
python-source = "python"
data = "wheel"
''')
    info = tmp_path/'demo-1.0.dist-info'
    info.mkdir()
    (info/'WHEEL').write_text('Wheel-Version: 1.0\nRoot-Is-Purelib: false\nTag: py3-none-any\n')
    (info/'METADATA').write_text('Metadata-Version: 2.1\nName: demo\nVersion: 1.0\n')
    (info/'entry_points.txt').write_text('[console_scripts]\ndemo = demo:main\n')
    with ZipFile(tmp_path/build_editable(tmp_path, metadata_directory=info)) as wheel:
        assert wheel.read('demo-1.0.pth').decode() == str(tmp_path/'python') + '\n'
        assert wheel.read('demo-1.0.dist-info/entry_points.txt') == (info/'entry_points.txt').read_bytes()
        record = list(csv.reader(io.StringIO(wheel.read('demo-1.0.dist-info/RECORD').decode())))
        assert {row[0] for row in record} == set(wheel.namelist())
        for path, digest, size in record[:-1]:
            data = wheel.read(path)
            assert digest == 'sha256=' + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b'=').decode()
            assert int(size) == len(data)
        assert record[-1] == ['demo-1.0.dist-info/RECORD', '', '']
