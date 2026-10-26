"""Narrow repair for the independently deployed standard-monthly VX collector."""
import argparse
import ast
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil


def repaired_source(source):
    tree = ast.parse(source)
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                 and node.name == 'get_vix_term_structure']
    if len(functions) != 1:
        raise ValueError('Expected one VX collector')
    function = functions[0]
    segment = ast.get_source_segment(source, function)
    old = "endDateTime=f'{as_of:%Y%m%d} 16:00:00 America/New_York'"
    new = ("endDateTime=pd.Timestamp(f'{as_of:%Y-%m-%d} 16:00:00', "
           "tz='America/New_York').to_pydatetime()")
    if new in segment and old not in segment:
        return source
    if segment.count(old) != 1 or 'aligned_curve(' not in segment:
        raise ValueError('Unknown collector version; no changes made')
    lines = source.splitlines(keepends=True)
    lines[function.lineno - 1:function.end_lineno] = [
        ''.join(lines[function.lineno - 1:function.end_lineno]).replace(old, new)]
    result = ''.join(lines)
    compile(result, '<repaired-collector>', 'exec')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('path', type=Path)
    parser.add_argument('--backup-dir', required=True, type=Path)
    args = parser.parse_args()
    source = args.path.read_text()
    result = repaired_source(source)
    if result == source:
        print(json.dumps({'status': 'ALREADY_FIXED'}))
        return
    args.backup_dir.mkdir(parents=True, exist_ok=True)
    backup = args.backup_dir / ('daily_post_close-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f') + '.py')
    shutil.copy2(args.path, backup)
    temporary = args.path.with_suffix('.vx-repair.tmp')
    with temporary.open('x') as stream:
        stream.write(result)
    shutil.copymode(args.path, temporary)
    os.replace(temporary, args.path)
    print(json.dumps({'status': 'FIXED', 'backup': str(backup),
                      'sha256': hashlib.sha256(args.path.read_bytes()).hexdigest()}))


if __name__ == '__main__':
    main()
