#!/usr/bin/env python3
"""track.py — 对比 skusnap 目录内相邻日期的商品快照，维护变更台账。

产出（均在输出目录下，台账按追加方式维护，重复运行幂等）：
  changes.csv      日期,商品号,变更字段,旧值,新值   仅真实变更，伪变更不入账
  增减登记.csv      日期,商品号,类型                类型=新增/下架
  summary.txt      按伪变更种类分别计数，一行一类
  .track_state     已处理快照对（去重用，不对外）

背景：最早一批快照成本以“元”记账，自某一天起改为“分”，切换前后同一成本
相差 100 倍，属于单位换算伪变更；数值还有带不带尾零两种写法，数值相同而
写法不同同样是伪变更。真实的调价、改名等与伪变更在切换日可能同时发生，
按归一化后的数值逐一判别。
"""

import argparse
import csv
import os
import sys
from decimal import Decimal, InvalidOperation

FIELDS = ('名称', '成本', '售价', '供应商', '状态')
NUMERIC_FIELDS = ('成本', '售价')

# 伪变更种类（summary.txt 的行标签）
KIND_UNIT = '成本单位换算（元转分）'
KIND_COST_TAIL = '成本尾零写法差异'
KIND_PRICE_TAIL = '售价尾零写法差异'


def parse_args():
    p = argparse.ArgumentParser(description='对比相邻商品快照，产出变更台账')
    p.add_argument('--snap-dir', default='skusnap', help='快照目录（默认 skusnap）')
    p.add_argument('--out-dir', default='.', help='台账输出目录（默认当前目录）')
    return p.parse_args()


def load_snapshots(snap_dir):
    """读取全部 sku_日期.csv，按日期排序返回 [(date, {商品号: row}), ...]。"""
    if not os.path.isdir(snap_dir):
        sys.exit('快照目录不存在: %s' % snap_dir)
    snaps = []
    for name in os.listdir(snap_dir):
        if not (name.startswith('sku_') and name.endswith('.csv')):
            continue
        date = name[4:-4]
        path = os.path.join(snap_dir, name)
        with open(path, newline='', encoding='utf-8-sig') as fh:
            rows = {}
            for row in csv.DictReader(fh):
                sku = row['商品号'].strip()
                rows[sku] = {k: (v.strip() if v is not None else '') for k, v in row.items()}
        snaps.append((date, rows))
    snaps.sort(key=lambda x: x[0])
    if len(snaps) < 2:
        sys.exit('快照不足两份，无法对比')
    return snaps


def to_decimal(text):
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def is_unit_switch(old_rows, new_rows):
    """判断这对快照是否为成本单位（元->分）切换：
    共同 SKU 中成本比值接近 100 的占绝对多数即为切换；单个 SKU 的百倍调价
    占比很低，不会触发。"""
    near100 = total = 0
    for sku, old in old_rows.items():
        new = new_rows.get(sku)
        if new is None:
            continue
        a, b = to_decimal(old['成本']), to_decimal(new['成本'])
        if a is None or b is None or a == 0:
            continue
        total += 1
        ratio = b / a
        if Decimal('95') < ratio < Decimal('105'):
            near100 += 1
    return total > 0 and near100 / total >= 0.5


def load_state(path):
    done = set()
    if os.path.exists(path):
        with open(path, encoding='utf-8') as fh:
            for line in fh:
                line = line.strip()
                if line:
                    done.add(tuple(line.split(',', 1)))
    return done


def load_existing_rows(path, ncols):
    """读取已有台账的行（去掉表头），用于防重复。"""
    rows = set()
    if os.path.exists(path):
        with open(path, newline='', encoding='utf-8-sig') as fh:
            reader = csv.reader(fh)
            for i, row in enumerate(reader):
                if i == 0 or not row:
                    continue
                rows.add(tuple(row[:ncols]))
    return rows


def append_rows(path, header, rows):
    new_file = not os.path.exists(path) or os.path.getsize(path) == 0
    with open(path, 'a', newline='', encoding='utf-8') as fh:
        writer = csv.writer(fh, lineterminator='\n')
        if new_file:
            writer.writerow(header)
        writer.writerows(rows)


def compare_pair(old_date, old_rows, new_date, new_rows):
    """对比一对快照，返回 (change_rows, ledger_rows, pseudo_counts)。
    change_rows: 真实变更；ledger_rows: 新增/下架；pseudo_counts: 伪变更加计数。"""
    change_rows = []
    ledger_rows = []
    pseudo = {KIND_UNIT: 0, KIND_COST_TAIL: 0, KIND_PRICE_TAIL: 0}
    unit_switch = is_unit_switch(old_rows, new_rows)

    # 字段顺序固定，保证同一 SKU 多行时输出确定
    for sku in sorted(set(old_rows) | set(new_rows)):
        old, new = old_rows.get(sku), new_rows.get(sku)
        if old is None:  # 当天首次出现
            ledger_rows.append((new_date, sku, '新增'))
            continue
        if new is None:  # 当天首次缺席
            ledger_rows.append((new_date, sku, '下架'))
            continue

        for field in FIELDS:
            old_raw, new_raw = old[field], new[field]
            if old_raw == new_raw:
                continue

            if field in NUMERIC_FIELDS:
                old_num, new_num = to_decimal(old_raw), to_decimal(new_raw)
                if old_num is not None and new_num is not None:
                    # 单位切换对：旧值（元）*100 后与新值（分）比较
                    scaled = old_num * 100 if (field == '成本' and unit_switch) else old_num
                    if scaled == new_num:
                        if field == '成本' and unit_switch:
                            pseudo[KIND_UNIT] += 1
                        elif field == '成本':
                            pseudo[KIND_COST_TAIL] += 1
                        else:
                            pseudo[KIND_PRICE_TAIL] += 1
                        continue  # 伪变更不入台账

            # 数值不同（真实调价）或非数值字段不同（真实改名/换供应商/改状态）
            change_rows.append((new_date, sku, field, old_raw, new_raw))

    return change_rows, ledger_rows, pseudo


def main():
    args = parse_args()
    os.makedirs(args.out_dir, exist_ok=True)
    changes_path = os.path.join(args.out_dir, 'changes.csv')
    ledger_path = os.path.join(args.out_dir, '增减登记.csv')
    summary_path = os.path.join(args.out_dir, 'summary.txt')
    state_path = os.path.join(args.out_dir, '.track_state')

    snaps = load_snapshots(args.snap_dir)
    done = load_state(state_path)
    existing_changes = load_existing_rows(changes_path, 5)
    existing_ledger = load_existing_rows(ledger_path, 3)

    new_state = []

    for (old_date, old_rows), (new_date, new_rows) in zip(snaps, snaps[1:]):
        pair = (old_date, new_date)
        if pair in done:
            continue  # 同一对快照不重复处理

        change_rows, ledger_rows, _ = compare_pair(old_date, old_rows,
                                                   new_date, new_rows)

        change_rows = [r for r in change_rows if r not in existing_changes]
        ledger_rows = [r for r in ledger_rows if r not in existing_ledger]
        if change_rows:
            append_rows(changes_path, ['日期', '商品号', '变更字段', '旧值', '新值'],
                        change_rows)
            existing_changes.update(change_rows)
        if ledger_rows:
            append_rows(ledger_path, ['日期', '商品号', '类型'], ledger_rows)
            existing_ledger.update(ledger_rows)
        new_state.append('%s,%s\n' % pair)

    if new_state:
        with open(state_path, 'a', encoding='utf-8') as fh:
            fh.writelines(new_state)

    # summary 每次运行对全部快照对重新统计，保证与快照现状一致
    summary_counts = {KIND_UNIT: 0, KIND_COST_TAIL: 0, KIND_PRICE_TAIL: 0}
    for (old_date, old_rows), (new_date, new_rows) in zip(snaps, snaps[1:]):
        _, _, pseudo = compare_pair(old_date, old_rows, new_date, new_rows)
        for k in summary_counts:
            summary_counts[k] += pseudo[k]
    with open(summary_path, 'w', encoding='utf-8') as fh:
        for kind in (KIND_UNIT, KIND_COST_TAIL, KIND_PRICE_TAIL):
            fh.write('%s %d\n' % (kind, summary_counts[kind]))

    print('处理快照 %d 份，快照对 %d 对，本次新处理 %d 对'
          % (len(snaps), len(snaps) - 1, len(new_state)))
    print('伪变更计数:', '；'.join('%s=%d' % (k, v) for k, v in summary_counts.items()))


if __name__ == '__main__':
    main()
