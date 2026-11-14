import { api } from '../api.js';
import { ENUMS, can, pct, riskTone, tagTone, wan } from '../config.js';
import { badge, drawer, emptyState, errorBox, errorMessage, esc, field, fmtTime, kv, table, toast } from '../ui.js';

const state = { q: '', riskLevel: '', tag: '', multi: '', sort: 'risk', threshold: '0.80' };

function cutoverTone(status) {
  return { completed: 'ok', running: 'warn', failed: 'danger', rolled_back: 'danger', pending: '' }[status] || '';
}

function cutoverPanel(ctx, cutover) {
  const canRun = can(ctx.user.role, 'run:create');
  if (!cutover) {
    return `<section class="panel">
      <div class="panel-head"><h2>归并规则切换</h2><span class="meta">当前：${esc(ENUMS.ruleProfile.v1_threshold)}</span></div>
      <div class="panel-body">
        <p class="note">归并规则可从单一相似度阈值升级为多规则复合门控。升级会对最近一个月房源整批回填，
        切换完成前银行估价清单仍使用上一版（v1），半成品不会进入导出。</p>
        ${canRun ? '<button class="btn sm accent" id="cutover-run">启动整批回填（v1 → v2）</button>' : ''}
      </div>
    </section>`;
  }
  const pctDone = cutover.total_batches ? Math.round((cutover.done_batches / cutover.total_batches) * 100) : 0;
  const recon = cutover.reconciliation || {};
  return `<section class="panel">
    <div class="panel-head">
      <h2>归并规则切换</h2>
      ${badge(cutoverTone(cutover.status), ENUMS.cutoverStatus[cutover.status] || cutover.status)}
    </div>
    <div class="panel-body" style="display:grid;gap:10px">
      <div class="kv">
        ${kv([
          ['规则版本', `${esc(ENUMS.ruleProfile[cutover.from_version] || cutover.from_version)} → ${esc(ENUMS.ruleProfile[cutover.to_version] || cutover.to_version)}`],
          ['回填窗口', `最近 ${esc(cutover.backfill_days)} 天 · ${esc(cutover.total_listings)} 条挂牌`],
          ['断点进度', `${esc(cutover.done_batches)} / ${esc(cutover.total_batches)} 批（${pctDone}%）`],
          ['复核处理', `沿用 ${esc(cutover.review_carried)} · 作废 ${esc(cutover.review_invalidated)} · 重排队 ${esc(cutover.review_requeued)}`],
          ['对账读数', `簇 ${esc(cutover.cluster_count)} · 成员 ${esc(cutover.member_count)} · 导出行 ${esc(cutover.export_rows)}${recon.consistent === false ? ' · <b style="color:var(--danger,#c0392b)">对账不一致</b>' : ''}`],
          ['最近错误', cutover.last_error ? esc(cutover.last_error) : '—'],
        ])}
      </div>
      <div class="risk-meter"><div class="track"><div class="fill ${cutoverTone(cutover.status)}" style="width:${Math.max(2, pctDone)}%"></div></div><span class="val">${pctDone}%</span></div>
      ${cutover.status === 'completed'
        ? '<p class="note">切换已完成：银行估价清单已切换到 v2，旧 v1 簇已归档保留追溯。</p>'
        : `<p class="note">切换未完成：导出仍只包含已发布的 ${esc(ENUMS.ruleProfile[cutover.from_version] || cutover.from_version)} 数据，staging 半成品被隔离。</p>
           ${canRun ? `<button class="btn sm accent" id="cutover-run">${cutover.status === 'failed' ? '从断点重试整批回填' : '继续整批回填'}</button>` : ''}`}
      ${(cutover.checkpoints || []).length ? `<details><summary class="muted" style="cursor:pointer">查看 ${cutover.checkpoints.length} 个批次断点</summary>
        <div class="run-history" style="margin-top:8px">${cutover.checkpoints.map((cp) => `<div class="row">
          <span class="mono">批次 #${esc(cp.batch_no)}</span>
          <span class="badge ${cp.status === 'done' ? 'ok' : cp.status === 'failed' ? 'danger' : 'warn'}">${esc(cp.status)}</span>
          <span class="muted">阶段 ${esc(cp.phase)} · 尝试 ${esc(cp.attempts)} 次 · ${esc(cp.listing_count)} 挂牌 / ${esc(cp.cluster_count)} 簇</span>
          ${cp.error ? `<span class="muted" style="color:var(--danger,#c0392b)">${esc(cp.error)}</span>` : ''}
        </div>`).join('')}</div></details>` : ''}
    </div>
  </section>`;
}

function riskBadge(prop) {
  if (!prop.risk_level || prop.risk_level === 'none') return '<span class="badge ok">正常</span>';
  return `<span class="badge ${riskTone(prop.risk_level)}">${esc(prop.risk_level_label)} ${esc(prop.risk_score)}</span>`;
}

function riskMeter(prop) {
  return `<div class="risk-meter">
    <div class="track"><div class="fill ${esc(prop.risk_level)}" style="width:${Math.max(2, prop.risk_score)}%"></div></div>
    <span class="val">${esc(prop.risk_score)}</span>
  </div>`;
}

function memberTable(members) {
  const rows = members
    .map((item) => {
      const dev = item.price_deviation || 0;
      const devClass = dev < -0.05 ? 'down' : dev > 0.05 ? 'up' : '';
      return `<tr data-id="${esc(item.id)}">
        <td>${esc(item.agency_name)}<div class="mono muted">${esc(item.lid)}</div></td>
        <td><div class="pair"><span class="raw">${esc(item.community_raw)}</span><span class="arrow">→</span><span class="norm">${esc(item.norm.community)}</span></div></td>
        <td><div class="pair"><span class="raw">${esc(item.area_raw)}</span><span class="arrow">→</span><span class="norm">${esc(item.norm.area ?? '—')}</span></div></td>
        <td><div class="pair"><span class="raw">${esc(item.price_raw)}</span><span class="arrow">→</span><span class="norm">${item.norm.price ? esc(wan(item.norm.price)) : '—'}</span></div></td>
        <td class="dev ${devClass}">${(dev * 100).toFixed(1)}%</td>
        <td>${(item.risk_tags || []).map((tag) => `<span class="badge ${tagTone(tag)}">${esc(ENUMS.riskTag[tag] || tag)}</span>`).join(' ') || esc(ENUMS.listingStatus[item.status] || item.status)}</td>
      </tr>`;
    })
    .join('');
  return `<div class="members"><table class="data compact">
    <thead><tr><th>中介 / 编号</th><th>小区 原文→归一</th><th>面积</th><th>价格</th><th>偏离中位</th><th>标签</th></tr></thead>
    <tbody>${rows}</tbody></table></div>`;
}

async function openCluster(view, ctx, summary) {
  let prop;
  try {
    prop = await api.get(`/api/properties/${summary.id}`);
  } catch (err) {
    toast(errorMessage(err), 'err');
    return;
  }
  const detail = prop.risk_detail || {};
  const { root } = drawer({
    title: `${prop.community} · ${prop.layout}`,
    subtitle: `簇 #${prop.id} · ${prop.member_count} 条挂牌 / ${prop.agency_count} 家中介 · 置信 ${prop.confidence.toFixed(3)}`,
    wide: true,
    body: `${kv([
      ['面积众数', `<span class="mono">${esc(prop.area_mode)} ㎡</span>（${esc(prop.area_min)}–${esc(prop.area_max)}）`],
      ['价格中位', `<span class="mono">${esc(wan(prop.price_median))}</span>（${esc(wan(prop.price_min))}–${esc(wan(prop.price_max))}）`],
      ['价差', `<span class="mono">${esc(prop.spread_wan)} 万</span> · 离散度 ${esc(prop.dispersion)}`],
      ['中介', esc((prop.agency_names || []).join(' / ') || '—')],
      ['风险分', `${riskMeter(prop)} ${badge(riskTone(prop.risk_level), prop.risk_level_label)}`],
      ['风险标签', (prop.risk_tags || []).map((tag) => `<span class="badge ${tagTone(tag)}">${esc(ENUMS.riskTag[tag] || tag)}</span>`).join(' ') || '—'],
      ['钓鱼价挂牌', (detail.bait_lids || []).length ? `<span class="mono">${esc(detail.bait_lids.join(' / '))}</span>` : '—'],
      ['僵尸挂牌', (detail.stale_lids || []).length ? `<span class="mono">${esc(detail.stale_lids.join(' / '))}</span>` : '—'],
      ['golden record 更新', fmtTime(prop.updated_at)],
    ])}
    <h3 style="margin:14px 0 8px;font-size:0.95rem">成员对照（算法 A 连边结果）</h3>
    ${memberTable(prop.members || [])}
    <p class="note">面积取众数、价格取中位、中介数去重计数；偏离 = (该条价格 − 簇中位) / 簇中位，低于 −10% 记为钓鱼价。</p>`,
    footer: `<button class="btn ghost" data-listings>查看该簇全部挂牌</button>`,
  });
  root.querySelector('[data-listings]')?.addEventListener('click', () => {
    ctx.navigate(`#/listings?property_id=${prop.id}`);
  });
}

function runPanel(ctx, latest) {
  const canRun = can(ctx.user.role, 'run:create');
  return `<section class="panel">
    <div class="panel-head">
      <h2>归并流水线</h2>
      <span class="meta">${latest ? `最近 ${esc(latest.run_no)} · ${fmtTime(latest.finished_at)}` : '尚未运行'}</span>
    </div>
    <div class="panel-body" style="display:grid;gap:10px">
      ${
        canRun
          ? `<div class="runbar" id="run-bar">
              ${field({ label: '连边阈值', name: 'threshold', type: 'number', value: state.threshold, attrs: 'step="0.01" min="0.50" max="1.00"' })}
              <button class="btn sm accent" id="run-merge">全量重算归并</button>
              <span class="muted" style="font-size:var(--fs-xs)">归一 → blocking → 相似度 → 并查集 → golden record → 风险分诊</span>
            </div>`
          : '<p class="note">只读角色可以查看归并结果，重算需要数据专员或管理员。</p>'
      }
      <div class="run-history" id="run-history"><span class="muted">读取运行记录…</span></div>
    </div>
  </section>`;
}

export const clustersPage = {
  async render(view, ctx) {
    let data;
    let runs;
    let cutover;
    try {
      [data, runs, cutover] = await Promise.all([
        api.get('/api/properties', {
          q: state.q,
          risk_level: state.riskLevel,
          tag: state.tag,
          multi: state.multi,
          sort: state.sort,
          limit: 50,
        }),
        api.get('/api/runs', { limit: 4 }),
        api.get('/api/cutover'),
      ]);
    } catch (err) {
      view.innerHTML = errorBox(errorMessage(err));
      view.querySelector('#retry-btn')?.addEventListener('click', () => clustersPage.render(view, ctx));
      return;
    }
    const latest = runs.items[0];
    const cutoverJob = cutover?.job || cutover;
    view.innerHTML = `
      ${cutoverPanel(ctx, cutoverJob)}
      ${runPanel(ctx, latest)}
      <section class="panel">
        <div class="panel-head">
          <h2>归并簇</h2>
          <div style="display:flex;gap:8px;align-items:center">
            <span class="meta">${data.total} 个簇${latest ? ` · 去重率 ${esc(pct(latest.dedup_rate))}` : ''}</span>
            ${can(ctx.user.role, 'export:read') ? '<button class="btn sm ghost" id="export-clusters">导出簇 CSV</button>' : ''}
          </div>
        </div>
        <div class="panel-body">
          <div class="filter-bar" id="cluster-filters">
            ${field({ label: '搜索', name: 'q', value: state.q, placeholder: '小区 / 户型' })}
            ${field({ label: '风险等级', name: 'risk_level', value: state.riskLevel, options: [{ value: '', label: '全部' }, ...Object.entries(ENUMS.riskLevel).map(([value, label]) => ({ value, label }))] })}
            ${field({ label: '风险标签', name: 'tag', value: state.tag, options: [{ value: '', label: '全部' }, ...Object.entries(ENUMS.riskTag).map(([value, label]) => ({ value, label }))] })}
            ${field({ label: '成员', name: 'multi', value: state.multi, options: [{ value: '', label: '全部' }, { value: 'true', label: '仅多成员簇' }] })}
            ${field({ label: '排序', name: 'sort', value: state.sort, options: [{ value: 'risk', label: '风险分' }, { value: 'spread', label: '价差' }, { value: 'members', label: '成员数' }, { value: 'recent', label: '更新时间' }] })}
          </div>
        </div>
        ${table({
          rows: data.items,
          columns: [
            { key: 'community', label: '小区 / 户型', render: (row) => `<b>${esc(row.community)}</b><div class="mono muted">${esc(row.layout)} · 簇 #${esc(row.id)}</div>` },
            { key: 'area_mode', label: '面积', className: 'num', render: (row) => `${esc(row.area_mode)} ㎡` },
            { key: 'price_median', label: '价格中位', className: 'num', render: (row) => esc(wan(row.price_median)) },
            { key: 'spread_wan', label: '价差', className: 'num', render: (row) => `${esc(row.spread_wan)} 万` },
            { key: 'member_count', label: '成员 / 中介', className: 'num', render: (row) => `${esc(row.member_count)} / ${esc(row.agency_count)}` },
            { key: 'confidence', label: '置信', className: 'num', render: (row) => row.confidence.toFixed(3) },
            { key: 'risk', label: '风险', render: (row) => `${riskBadge(row)} ${(row.risk_tags || []).map((tag) => `<span class="badge ${tagTone(tag)}">${esc(ENUMS.riskTag[tag] || tag)}</span>`).join(' ')}` },
          ],
          empty: { title: '没有匹配的簇', hint: '调整筛选，或先跑一次全量重算。' },
        })}
      </section>`;

    const filters = view.querySelector('#cluster-filters');
    filters?.addEventListener('change', () => {
      state.q = filters.querySelector('[name=q]').value.trim();
      state.riskLevel = filters.querySelector('[name=risk_level]').value;
      state.tag = filters.querySelector('[name=tag]').value;
      state.multi = filters.querySelector('[name=multi]').value;
      state.sort = filters.querySelector('[name=sort]').value;
      clustersPage.render(view, ctx);
    });
    filters?.querySelector('[name=q]')?.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        state.q = event.target.value.trim();
        clustersPage.render(view, ctx);
      }
    });
    const history = view.querySelector('#run-history');
    if (history) {
      history.innerHTML = runs.items.length
        ? runs.items
            .map(
              (run) => `<div class="row">
                <span class="mono">${esc(run.run_no)}</span>
                <span>${esc(run.trigger_label)}</span>
                <span class="muted">${esc(run.cluster_count)} 簇 / ${esc(run.multi_count)} 多成员 · 风险 ${esc(run.risk_count)} · ${esc(pct(run.dedup_rate))}</span>
                <span class="mono muted">${esc((run.metrics || {}).duration_ms ?? '—')} ms</span>
              </div>`,
            )
            .join('')
        : emptyState({ title: '还没有运行记录', hint: '点「全量重算归并」跑第一次。' });
    }
    const runButton = view.querySelector('#run-merge');
    runButton?.addEventListener('click', async () => {
      const input = view.querySelector('[name=threshold]');
      state.threshold = input?.value || '0.80';
      runButton.disabled = true;
      runButton.textContent = '重算中…';
      try {
        const run = await api.post('/api/runs', { trigger: 'manual', threshold: Number(state.threshold) });
        toast(`${run.run_no}：${run.cluster_count} 簇 / 多成员 ${run.multi_count} / 风险 ${run.risk_count}`);
        await clustersPage.render(view, ctx);
      } catch (err) {
        toast(errorMessage(err), 'err');
        runButton.disabled = false;
        runButton.textContent = '全量重算归并';
      }
    });
    const cutoverButton = view.querySelector('#cutover-run');
    cutoverButton?.addEventListener('click', async () => {
      cutoverButton.disabled = true;
      cutoverButton.textContent = '整批回填中…';
      try {
        const job = await api.post('/api/cutover/run', { job_key: 'rule-v1-to-v2', backfill_days: 30 });
        if (job.status === 'completed') {
          toast(`切换完成：${job.cluster_count} 簇 / 成员 ${job.member_count} / 导出行 ${job.export_rows} 对账一致`);
        } else if (job.status === 'failed') {
          toast(`批次失败，旧版仍生效：${job.last_error || '见断点详情'}`, 'err');
        } else {
          toast(`回填进度 ${job.done_batches}/${job.total_batches} 批`);
        }
        await clustersPage.render(view, ctx);
      } catch (err) {
        toast(errorMessage(err), 'err');
        cutoverButton.disabled = false;
        cutoverButton.textContent = '重试整批回填';
      }
    });
    view.querySelectorAll('table.data tbody tr').forEach((row) => {
      row.addEventListener('click', () => {
        const item = data.items.find((entry) => String(entry.id) === row.dataset.id);
        if (item) openCluster(view, ctx, item);
      });
    });
    view.querySelector('#export-clusters')?.addEventListener('click', async () => {
      try {
        await api.download('/api/exports/clusters.csv', 'clusters.csv');
        toast('已开始下载归并簇清单');
      } catch (err) {
        toast(errorMessage(err), 'err');
      }
    });
    if (ctx.query.focus) {
      const target = data.items.find((entry) => String(entry.id) === ctx.query.focus);
      const summary = target || (await api.get(`/api/properties/${ctx.query.focus}`));
      openCluster(view, ctx, summary);
    }
  },
};

export default clustersPage;
