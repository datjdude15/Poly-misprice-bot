import csv
import json
import math
import os
from collections import defaultdict
from statistics import mean

CLOSED_TRADES_FILE = os.environ.get('CLOSED_TRADES_FILE', 'closed_trades.csv')
OPEN_TRADES_FILE = os.environ.get('OPEN_TRADES_FILE', 'open_trades.csv')
SUMMARY_OUT_FILE = os.environ.get('SUMMARY_OUT_FILE', 'analytics_summary.json')


def read_csv_rows(path: str):
    if not os.path.exists(path):
        return []
    with open(path, 'r', newline='') as f:
        return list(csv.DictReader(f))


def to_float(val, default=None):
    try:
        if val is None or val == '':
            return default
        return float(val)
    except Exception:
        return default


def bucket_edge(edge):
    if edge is None:
        return 'unknown'
    if edge < 25:
        return '<25c'
    if edge < 35:
        return '25-34.99c'
    if edge < 45:
        return '35-44.99c'
    if edge < 55:
        return '45-54.99c'
    return '55c+'


def bucket_momentum(mom):
    if mom is None:
        return 'unknown'
    if mom < 20:
        return '0-19.9'
    if mom < 40:
        return '20-39.9'
    if mom < 60:
        return '40-59.9'
    if mom < 80:
        return '60-79.9'
    return '80-100'


def parse_hour_from_slug(slug: str):
    if not slug:
        return 'unknown'
    parts = slug.split('-')
    for part in parts:
        if part.endswith('am') or part.endswith('pm'):
            return part
    return 'unknown'


def avg(vals):
    vals = [v for v in vals if v is not None]
    return round(mean(vals), 4) if vals else None


def win_rate(rows, field, win_value='WIN'):
    usable = [r for r in rows if r.get(field)]
    if not usable:
        return None
    wins = sum(1 for r in usable if r.get(field) == win_value)
    return round(wins / len(usable), 4)


def summarize_group(rows, result_field='scalp_status'):
    usable = [r for r in rows if r.get(result_field) in ('WIN', 'LOSS')]
    if not usable:
        return {
            'count': 0,
            'wins': 0,
            'losses': 0,
            'win_rate': None,
            'avg_pnl_pct': None,
        }
    wins = sum(1 for r in usable if r.get(result_field) == 'WIN')
    losses = sum(1 for r in usable if r.get(result_field) == 'LOSS')
    pnl_vals = [to_float(r.get('scalp_pnl_pct')) for r in usable]
    return {
        'count': len(usable),
        'wins': wins,
        'losses': losses,
        'win_rate': round(wins / len(usable), 4),
        'avg_pnl_pct': avg(pnl_vals),
    }


def main():
    closed_rows = read_csv_rows(CLOSED_TRADES_FILE)
    open_rows = read_csv_rows(OPEN_TRADES_FILE)

    usable_scalps = [r for r in closed_rows if r.get('scalp_status') in ('WIN', 'LOSS')]
    usable_settles = [r for r in closed_rows if r.get('settle_result') in ('WIN', 'LOSS')]

    summary = {
        'files': {
            'closed_trades_file': CLOSED_TRADES_FILE,
            'open_trades_file': OPEN_TRADES_FILE,
            'summary_out_file': SUMMARY_OUT_FILE,
        },
        'headline': {
            'open_trade_count': len(open_rows),
            'closed_trade_count': len(closed_rows),
            'scalp_closed_count': len(usable_scalps),
            'settled_count': len(usable_settles),
            'scalp_win_rate': win_rate(closed_rows, 'scalp_status'),
            'settle_win_rate': win_rate(closed_rows, 'settle_result'),
            'avg_scalp_pnl_pct': avg([to_float(r.get('scalp_pnl_pct')) for r in usable_scalps]),
            'avg_edge_cents': avg([to_float(r.get('edge_cents')) for r in usable_scalps]),
            'avg_momentum': avg([to_float(r.get('momentum')) for r in usable_scalps]),
        },
        'by_grade': {},
        'by_action': {},
        'by_hour': {},
        'by_edge_bucket': {},
        'by_momentum_bucket': {},
        'time_exit': {},
        'tp_sl_breakdown': {},
        'best_segments': {},
    }

    grade_groups = defaultdict(list)
    action_groups = defaultdict(list)
    hour_groups = defaultdict(list)
    edge_groups = defaultdict(list)
    momentum_groups = defaultdict(list)
    exit_reason_groups = defaultdict(list)

    for row in usable_scalps:
        grade_groups[row.get('grade', 'unknown')].append(row)
        action_groups[row.get('action', 'unknown')].append(row)
        hour_groups[parse_hour_from_slug(row.get('slug', ''))].append(row)
        edge_groups[bucket_edge(to_float(row.get('edge_cents')))].append(row)
        momentum_groups[bucket_momentum(to_float(row.get('momentum')))].append(row)
        exit_reason_groups[row.get('scalp_exit_reason', 'unknown')].append(row)

    for key, rows in sorted(grade_groups.items()):
        summary['by_grade'][key] = summarize_group(rows)
    for key, rows in sorted(action_groups.items()):
        summary['by_action'][key] = summarize_group(rows)
    for key, rows in sorted(hour_groups.items()):
        summary['by_hour'][key] = summarize_group(rows)
    for key, rows in sorted(edge_groups.items()):
        summary['by_edge_bucket'][key] = summarize_group(rows)
    for key, rows in sorted(momentum_groups.items()):
        summary['by_momentum_bucket'][key] = summarize_group(rows)

    summary['time_exit'] = summarize_group(exit_reason_groups.get('TIME_EXIT', []))
    summary['tp_sl_breakdown'] = {
        'TP': summarize_group(exit_reason_groups.get('TP', [])),
        'SL': summarize_group(exit_reason_groups.get('SL', [])),
        'TIME_EXIT': summarize_group(exit_reason_groups.get('TIME_EXIT', [])),
    }

    def best_key(group_dict):
        ranked = []
        for k, v in group_dict.items():
            if v['count'] >= 3 and v['win_rate'] is not None:
                ranked.append((v['win_rate'], v['count'], k, v))
        ranked.sort(reverse=True)
        return ranked[0] if ranked else None

    best_grade = best_key(summary['by_grade'])
    best_hour = best_key(summary['by_hour'])
    best_edge = best_key(summary['by_edge_bucket'])
    best_mom = best_key(summary['by_momentum_bucket'])

    summary['best_segments'] = {
        'best_grade': {'key': best_grade[2], 'stats': best_grade[3]} if best_grade else None,
        'best_hour': {'key': best_hour[2], 'stats': best_hour[3]} if best_hour else None,
        'best_edge_bucket': {'key': best_edge[2], 'stats': best_edge[3]} if best_edge else None,
        'best_momentum_bucket': {'key': best_mom[2], 'stats': best_mom[3]} if best_mom else None,
    }

    with open(SUMMARY_OUT_FILE, 'w') as f:
        json.dump(summary, f, indent=2)

    print('=== POLY ANALYTICS SUMMARY ===')
    print(json.dumps(summary['headline'], indent=2))
    print(f"Saved full analytics -> {SUMMARY_OUT_FILE}")


if __name__ == '__main__':
    main()
