/**
 * Drawflow 格式 ⇄ 引擎正規格式的轉換。
 *
 * 這是唯一知道 Drawflow 資料長相的地方 —— 之後想換成 LiteGraph（ComfyUI 用的
 * 那個）只要重寫這個檔案，引擎與後端完全不用動。
 *
 * Drawflow 的連線命名很容易搞錯（已對 0.0.60 的原始碼確認）：
 *   outputs.output_1.connections[] = { node: "<目標 id>", output: "input_1" }
 *                                                          ^^^^^^ 這是對方的 input
 *   inputs.input_1.connections[]   = { node: "<來源 id>", input: "output_1" }
 *                                                         ^^^^^ 這是對方的 output
 * 也就是說欄位名指的是「對面那端的 port」，不是自己這端的。
 *
 * port 對應：條件節點 output_1 = true、output_2 = false；其餘節點 output_1 = out。
 */

export const CONDITION_PORTS = ['true', 'false'];
export const DEFAULT_PORT = 'out';

/** 某個節點型別有哪些出口 port。 */
export function portsOf(type) {
  return type === 'condition' ? CONDITION_PORTS : [DEFAULT_PORT];
}

/** 這個型別需要入口嗎？需求節點是起點，不畫入口。 */
export function hasInput(type) {
  return type !== 'requirement';
}

const OPTIONAL_KEYS = ['mutates', 'max_visits', 'timeout_sec', 'join', 'on_error'];
const DEFAULTS = { join: 'all', on_error: 'fail' };

// ---------------------------------------------------------------- 正規 → Drawflow

export function toDrawflow(graph, renderHtml) {
  const data = {};
  const idMap = new Map();
  const typeOf = new Map();

  graph.nodes.forEach((node, index) => {
    idMap.set(node.id, index + 1);
    typeOf.set(node.id, node.type);
  });

  for (const node of graph.nodes) {
    const dfId = idMap.get(node.id);
    const outs = portsOf(node.type);

    const outputs = {};
    outs.forEach((_, i) => { outputs[`output_${i + 1}`] = { connections: [] }; });

    const inputs = {};
    if (hasInput(node.type)) inputs.input_1 = { connections: [] };

    // 把整個正規節點塞進 Drawflow 的 data 欄位，往返時才不會掉資訊
    const payload = { nid: node.id, label: node.label || node.id, config: node.config || {} };
    for (const key of OPTIONAL_KEYS) {
      if (node[key] !== undefined && node[key] !== null) payload[key] = node[key];
    }

    data[dfId] = {
      id: dfId,
      name: node.type,
      data: payload,
      class: `nd nd-${node.type}`,
      html: renderHtml ? renderHtml(node) : node.label || node.id,
      typenode: false,
      inputs,
      outputs,
      pos_x: node.pos?.x ?? 80 + (dfId % 5) * 220,
      pos_y: node.pos?.y ?? 80 + Math.floor(dfId / 5) * 160,
    };
  }

  for (const edge of graph.edges || []) {
    const src = idMap.get(edge.from);
    const dst = idMap.get(edge.to);
    if (src === undefined || dst === undefined) continue;

    const port = edge.port || DEFAULT_PORT;
    const idx = portsOf(typeOf.get(edge.from)).indexOf(port);
    if (idx < 0) continue;
    const outKey = `output_${idx + 1}`;

    if (!data[src].outputs[outKey]) continue;
    if (!data[dst].inputs.input_1) continue;

    data[src].outputs[outKey].connections.push({ node: String(dst), output: 'input_1' });
    data[dst].inputs.input_1.connections.push({ node: String(src), input: outKey });
  }

  return { drawflow: { Home: { data } } };
}

// ---------------------------------------------------------------- Drawflow → 正規

export function fromDrawflow(exported, meta = {}) {
  const raw = exported?.drawflow?.Home?.data || {};
  const nodes = [];
  const edges = [];
  const nidOf = new Map();

  for (const [dfId, node] of Object.entries(raw)) {
    // 舊資料或手動拉出來的節點可能沒有 nid，用 Drawflow 的數字 id 補
    const nid = node.data?.nid || `n${dfId}`;
    nidOf.set(String(dfId), nid);
  }

  for (const [dfId, node] of Object.entries(raw)) {
    const payload = node.data || {};
    const canonical = {
      id: nidOf.get(String(dfId)),
      type: node.name,
      label: payload.label || nidOf.get(String(dfId)),
      config: payload.config || {},
      pos: { x: node.pos_x, y: node.pos_y },
    };
    for (const key of OPTIONAL_KEYS) {
      const value = payload[key];
      if (value === undefined || value === null) continue;
      if (DEFAULTS[key] !== undefined && value === DEFAULTS[key]) continue;
      canonical[key] = value;
    }
    nodes.push(canonical);
  }

  // 只讀 outputs 一側，避免同一條邊被兩邊各記一次
  for (const [dfId, node] of Object.entries(raw)) {
    const ports = portsOf(node.name);
    for (const [outKey, out] of Object.entries(node.outputs || {})) {
      const idx = Number(outKey.split('_')[1]) - 1;
      const port = ports[idx] ?? DEFAULT_PORT;
      for (const conn of out.connections || []) {
        const target = nidOf.get(String(conn.node));
        if (!target) continue;
        const edge = { from: nidOf.get(String(dfId)), to: target };
        if (port !== DEFAULT_PORT) edge.port = port;
        edges.push(edge);
      }
    }
  }

  return {
    id: meta.id || '',
    name: meta.name || '',
    settings: meta.settings || {},
    nodes,
    edges,
  };
}

/** 產生不會撞到的節點 id。 */
export function makeNodeId(type, existing) {
  const base = type.replace(/[^a-z0-9]/gi, '') || 'node';
  let n = 1;
  let candidate = base;
  while (existing.has(candidate)) candidate = `${base}${++n}`;
  return candidate;
}
