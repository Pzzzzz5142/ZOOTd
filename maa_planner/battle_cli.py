"""Explicit public battle data fetch and offline analysis; never starts MAA."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

from .battle_data import _source, analyze_battle, fetch_battle, parse_battle, render_ascii
from .battle_solver import analyze_candidates, compile_copilot
from .battle_generator import generate_plan
from .battle_portfolio import compare_plans
from .battle_strategies import strategy_catalog
from .combat_model import evaluate_plan
from .combat_effects import effect_rules
from .copilot_static import build_catalog
from .operator_box import ModuleProgress, Operator, OperatorBox, SkillProgress
from .prts import PrtsError, decode, require
from .util import atomic_write_json, canonical_json, sha256_bytes


def read_box(path):
    """Read the normalized private snapshot, preserving unknown progression."""
    raw = path.read_bytes()
    value = decode(raw)
    require(isinstance(value, dict) and type(value.get('schema')) is int
            and value['schema'] == 1 and isinstance(value.get('operators'), dict), 'Invalid Box snapshot.')
    for field in ('source', 'fetched_at', 'account_id'):
        require(isinstance(value.get(field), str) and bool(value[field]), 'Invalid Box metadata.')
    operators = {}
    for key, row in value['operators'].items():
        require(isinstance(row, dict) and row.get('id') == key, 'Invalid Box operator identity.')
        for field, minimum, maximum in (('elite', 0, 2), ('level', 1, 90), ('potential', 1, 6),
                                        ('main_skill_level', 1, 7)):
            item = row.get(field)
            require(item is None or type(item) is int and minimum <= item <= maximum,
                    'Invalid Box progression.')
        skills, modules = row.get('skills'), row.get('modules')
        require(skills is None or isinstance(skills, dict), 'Invalid Box skills.')
        require(modules is None or isinstance(modules, dict), 'Invalid Box modules.')
        if skills is not None:
            for progress in skills.values():
                require(isinstance(progress, dict) and (progress.get('mastery') is None
                        or type(progress['mastery']) is int and 0 <= progress['mastery'] <= 3),
                        'Invalid Box skill mastery.')
            skills = {name: SkillProgress(progress.get('mastery')) for name, progress in skills.items()}
        if modules is not None:
            for progress in modules.values():
                require(isinstance(progress, dict) and (progress.get('level') is None
                        or type(progress['level']) is int and 0 <= progress['level'] <= 3)
                        and (progress.get('unlocked') is None or type(progress['unlocked']) is bool),
                        'Invalid Box module progression.')
            modules = {name: ModuleProgress(progress.get('level'), progress.get('unlocked'))
                       for name, progress in modules.items()}
        trust = row.get('favor_percent')
        require(trust is None or type(trust) is int and 0 <= trust <= 200, 'Invalid Box trust')
        selected_equip = row.get('default_equip_id')
        require(selected_equip is None or isinstance(selected_equip, str) and bool(selected_equip),
                'Invalid Box selected equipment')
        operators[key] = Operator(key, row.get('elite'), row.get('level'), row.get('potential'),
                                  row.get('main_skill_level'), skills, modules, trust, selected_equip)
    return OperatorBox(value['source'], value['fetched_at'], value['account_id'], operators), {
        'path': str(path.resolve()), 'sha256': sha256_bytes(raw), 'fetched_at': value['fetched_at']}


def static_inputs(root, graph, *, offline=False):
    revision = graph.get('sources', {}).get('navigation', {}).get('revision')
    require(isinstance(revision, str) and re.fullmatch('[0-9a-f]{40}', revision),
            'Compilation requires a pinned game revision in graph sources.')
    directory = root / 'var/cache/battle-data' / revision / 'static'
    tables, evidence = {}, {}
    for name in ('character_table', 'uniequip_table'):
        filename = name + '.json'
        require(not offline or (directory / filename).is_file()
                and (directory / (filename + '.source.json')).is_file(),
                'Static sources are not cached; run battle-solver fetch first.')
        url = f'https://raw.githubusercontent.com/Kengxxiao/ArknightsGameData/{revision}/zh_CN/gamedata/excel/{filename}'
        tables[name], evidence[name] = _source(directory, filename, url)
    battle = next((root / layer / 'resource/battle_data.json' for layer in
                   ('var/data/cache', 'var/data/MaaResource', 'var/data')
                   if (root / layer / 'resource/battle_data.json').is_file()), None)
    require(battle is not None, 'Installed MAA operator identities are missing.')
    raw = battle.read_bytes()
    evidence['maa_battle_data'] = {'path': str(battle), 'sha256': sha256_bytes(raw)}
    catalog = build_catalog(tables['character_table'], tables['uniequip_table'], decode(raw))
    return catalog, tables['character_table'], evidence


def combat_inputs(root, graph, *, offline=False):
    revision = graph['sources']['navigation']['revision']
    require(isinstance(revision, str) and re.fullmatch('[0-9a-f]{40}', revision),
            'Combat sources require a pinned game revision')
    directory = root / 'var/cache/battle-data' / revision / 'static'
    tables, evidence = {}, {}
    paths = {name: 'excel/' + name + '.json' for name in
             ('skill_table', 'range_table', 'battle_equip_table', 'uniequip_table')}
    paths.update(buff_table='buff_table.json', ep_breakbuff_table='battle/ep_breakbuff_table.json')
    for name, source_path in paths.items():
        filename = name + '.json'
        require(not offline or (directory / filename).is_file()
                and (directory / (filename + '.source.json')).is_file(), 'Combat source is not cached: ' + name)
        url = f'https://raw.githubusercontent.com/Kengxxiao/ArknightsGameData/{revision}/zh_CN/gamedata/{source_path}'
        tables[name], evidence[name] = _source(directory, filename, url)
    tables['effect_rules'] = effect_rules(tables['buff_table'], tables['ep_breakbuff_table'])
    return tables, evidence


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-root', type=Path, default=Path.cwd())
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('strategies', help='List reviewed offline solver strategies')
    fetch = commands.add_parser('fetch', help='Fetch public sources pinned to navigation revision')
    fetch.add_argument('stage')
    fetch.add_argument('--raid', action='store_true', help='Read explicit challenge (FOUR_STAR) identity and mechanics')
    fetch.add_argument('--refresh', action='store_true', help='Redownload pinned immutable sources')
    fetch.add_argument('--combat', action='store_true', help='Also cache combat and effect rules for offline evaluation')
    fetch.add_argument('--output', type=Path)
    analyze = commands.add_parser('analyze', help='Analyze local graph or raw local level/enemy sources')
    inputs = analyze.add_mutually_exclusive_group(required=True)
    inputs.add_argument('--graph', type=Path)
    inputs.add_argument('--level', type=Path)
    analyze.add_argument('--enemy-database', type=Path)
    analyze.add_argument('--stage-id')
    analyze.add_argument('--level-id')
    analyze.add_argument('--code')
    analyze.add_argument('--format', choices=('json', 'ascii'), default='json')
    analyze.add_argument('--candidates', action='store_true', help='Rank geometric deployment coverage')
    analyze.add_argument('--placement', choices=('MELEE', 'RANGED', 'ALL'), default='MELEE')
    analyze.add_argument('--limit', type=int, default=20)
    strategy_ids = [s['id'] for s in strategy_catalog()]
    for name in ('compile', 'validate', 'evaluate', 'generate', 'compare'):
        child = commands.add_parser(name, help='Check a local plan or Copilot against graph and Box')
        child.add_argument('--graph', type=Path, required=True)
        child.add_argument('--box', type=Path)
        child.add_argument('--offline', action='store_true', help='Require already cached public static sources')
        if name in ('generate', 'compare'):
            child.add_argument('--research', type=Path, required=True,
                               help='Graph-bound non-tactical mechanic notes and sources')
            child.add_argument('--output', type=Path, required=True, help='New private artifact directory')
            child.add_argument('--horizon', type=float, default=120)
            child.add_argument('--max-candidates', type=int, default=8,
                               help='Per-strategy formation attempt limit, 1..64')
            child.add_argument('--branch-width', type=int, default=3,
                               help='Ranked alternatives per decision, 2..8')
            if name == 'generate':
                child.add_argument('--strategy', choices=strategy_ids, default='greedy')
            else:
                child.add_argument('--strategies', nargs='+', choices=strategy_ids, default=strategy_ids)
                child.add_argument('--workers', type=int, default=2,
                                   help='Independent search processes, 1..8; 1 runs sequentially')
        elif name in ('compile', 'evaluate'):
            child.add_argument('--plan', type=Path, required=True)
            child.add_argument('--output', type=Path, required=name == 'compile')
            if name == 'evaluate':
                child.add_argument('--horizon', type=float, default=60,
                                   help='Seconds in the explicit continuous-contact scenario (not global wave time)')
        else:
            child.add_argument('--copilot', type=Path, required=True)
            child.add_argument('--plan', type=Path, required=True,
                               help='Bind researched mechanic acknowledgements to this candidate')
    args = parser.parse_args(argv)
    try:
        if args.command == 'strategies':
            output = {'schema': 1, 'strategies': strategy_catalog()}
        elif args.command == 'fetch':
            graph = fetch_battle(args.project_root, args.stage, refresh=args.refresh, raid=args.raid)
            static_inputs(args.project_root, graph)
            if args.combat:
                combat_inputs(args.project_root, graph)
            if args.output:
                atomic_write_json(args.output, graph, mode=0o600)
            output = graph
        elif args.command == 'analyze':
            if args.graph:
                graph = decode(args.graph.read_bytes())
            else:
                if not all((args.enemy_database, args.stage_id, args.level_id, args.code)):
                    parser.error('--level requires --enemy-database, --stage-id, --level-id and --code')
                level_raw, enemy_raw = args.level.read_bytes(), args.enemy_database.read_bytes()
                identity = {'stage_id': args.stage_id, 'battle_id': args.stage_id,
                            'level_id': args.level_id, 'code': args.code, 'difficulty': 'NORMAL'}
                sources = {'level': {'path': str(args.level.resolve()), 'sha256': sha256_bytes(level_raw)},
                           'enemy_database': {'path': str(args.enemy_database.resolve()),
                                              'sha256': sha256_bytes(enemy_raw)}}
                graph = parse_battle(decode(level_raw), decode(enemy_raw), identity=identity, sources=sources)
            output = (analyze_candidates(graph, placement=args.placement, limit=args.limit)
                      if args.candidates else render_ascii(graph) if args.format == 'ascii'
                      else analyze_battle(graph))
        else:
            graph_raw = args.graph.read_bytes()
            graph = decode(graph_raw)
            box, box_evidence = read_box(args.box or args.project_root / 'var/state/operator-box.json')
            catalog, chars, sources = static_inputs(args.project_root, graph, offline=args.offline)
            if args.command in ('generate', 'compare'):
                require(not args.output.exists(), 'Generation output directory already exists; use a new candidate.')
                combat, combat_sources = combat_inputs(args.project_root, graph, offline=args.offline)
                research_raw = args.research.read_bytes()
                sources.update(combat_sources)
                evidence = {'inputs': {'box': box_evidence, 'static': sources,
                    'graph_file_sha256': sha256_bytes(graph_raw),
                    'research_file_sha256': sha256_bytes(research_raw)}}
                evidence['code'] = {'head': subprocess.check_output(
                    ['git', '-C', str(args.project_root), 'rev-parse', 'HEAD'], text=True).strip(),
                    'dirty': bool(subprocess.check_output(['git', '-C', str(args.project_root),
                        'status', '--porcelain'], text=True).strip())}
                evidence['entrypoint'] = ['battle-solver', args.command, '--graph', str(args.graph.resolve()),
                    '--research', str(args.research.resolve()), '--box', box_evidence['path'],
                    '--horizon', str(args.horizon), '--max-candidates', str(args.max_candidates),
                    '--branch-width', str(args.branch_width), '--output', str(args.output.resolve())
                    ] + (['--offline'] if args.offline else [])
                options = dict(research=decode(research_raw), equipment=combat['battle_equip_table'],
                    equip_metadata=combat['uniequip_table'], horizon=args.horizon,
                    effect_rules=combat['effect_rules'],
                    max_candidates=args.max_candidates, branch_width=args.branch_width)
                if args.command == 'generate':
                    evidence['entrypoint'] += ['--strategy', args.strategy]
                    generated = generate_plan(graph, box, catalog, chars,
                        combat['skill_table'], combat['range_table'], strategy=args.strategy, **options)
                    candidates = {args.strategy: generated}
                    output = generated['generation']
                else:
                    evidence['entrypoint'] += ['--strategies', *args.strategies, '--workers', str(args.workers)]
                    compared = compare_plans(graph, box, catalog, chars,
                        combat['skill_table'], combat['range_table'], strategies=args.strategies,
                        workers=args.workers, **options)
                    candidates, output = compared['candidates'], compared['comparison']
                output.update(evidence, output=str(args.output.resolve()))
                args.output.mkdir(parents=True, mode=0o700)
                for name, generated in candidates.items():
                    directory = args.output if args.command == 'generate' else args.output / name
                    if args.command == 'compare':
                        directory.mkdir(mode=0o700)
                    generated['generation'].update(evidence, output=str(directory.resolve()))
                    for key in ('plan', 'copilot', 'combat', 'generation'):
                        atomic_write_json(directory / (key + '.json'), generated[key], mode=0o600)
                if args.command == 'compare':
                    for row in output['results']:
                        if row['status'] == 'candidate':
                            row['copilot_file'] = str((args.output / row['strategy'] / 'copilot.json').resolve())
                    atomic_write_json(args.output / 'comparison.json', output, mode=0o600)
                print(json.dumps(output, ensure_ascii=False, indent=2, allow_nan=False))
                return 0 if candidates else 1
            plan_raw = args.plan.read_bytes()
            plan = decode(plan_raw)
            if args.command in ('compile', 'evaluate'):
                result = compile_copilot(graph, plan, box, catalog, character_table=chars)
                output = result['analysis']
                if args.command == 'compile':
                    atomic_write_json(args.output, result['copilot'], mode=0o600)
                else:
                    combat, combat_sources = combat_inputs(args.project_root, graph, offline=args.offline)
                    output = evaluate_plan(graph, result['copilot'], box, catalog, chars,
                        combat['skill_table'], combat['range_table'], horizon=args.horizon,
                        equipment=combat['battle_equip_table'], equip_metadata=combat['uniequip_table'],
                        effect_rules=combat['effect_rules'])
                    sources.update(combat_sources)
            else:
                expected = compile_copilot(graph, plan, box, catalog, character_table=chars)
                content = decode(args.copilot.read_bytes())
                require(canonical_json(content) == canonical_json(expected['copilot']),
                        'Copilot differs from the compiled, researched plan.')
                output = expected['analysis']
            output['inputs'] = {'box': box_evidence, 'static': sources,
                               'graph_sha256': sha256_bytes(args.graph.read_bytes()),
                               'plan_sha256': sha256_bytes(plan_raw)}
            if args.command == 'compile':
                output['output'] = str(args.output.resolve())
                atomic_write_json(args.output.with_suffix('.analysis.json'), output, mode=0o600)
            elif args.command == 'evaluate' and args.output:
                atomic_write_json(args.output, output, mode=0o600)
        print(output if isinstance(output, str) else json.dumps(output, ensure_ascii=False,
                                                              indent=2, allow_nan=False))
        return 0
    except (PrtsError, OSError, KeyError, TypeError, ValueError) as exc:
        print(json.dumps({'status': 'error', 'category': getattr(exc, 'category', 'battle_data'),
                          'message': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
