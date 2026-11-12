(() => {
  const NS = 'http://www.w3.org/2000/svg'
  const WIDTH = 1440
  const HEIGHT = 650
  const COLORS = ['#236b82', '#3b8f7a', '#7184bd', '#d28a48', '#c56b77', '#708c4c']
  const DARK = '#183b50'
  const MUTED = '#62798a'
  const LINE = '#bfd0da'
  const PALE = '#eef4f7'
  const existingTypes = new Set(['concept_map', 'tree', 'process', 'timeline', 'comparison', 'cycle', 'pie'])

  function node(tag, attrs, parent) {
    const item = document.createElementNS(NS, tag)
    Object.entries(attrs || {}).forEach(([key, value]) => item.setAttribute(key, String(value)))
    if (parent) parent.appendChild(item)
    return item
  }

  function text(parent, value, x, y, options) {
    const opts = options || {}
    const size = opts.size || 19
    const maxChars = opts.maxChars || Math.max(4, Math.floor((opts.width || 240) / (size * 1.08)))
    const lines = []
    const words = Array.from(String(value || ''))
    while (words.length) lines.push(words.splice(0, maxChars).join(''))
    if (!lines.length) lines.push('')
    const el = node('text', {
      x, y, fill: opts.color || DARK, 'font-size': size,
      'font-family': 'Microsoft YaHei, PingFang SC, sans-serif',
      'font-weight': opts.weight || 500,
      'text-anchor': opts.anchor || 'start',
    }, parent)
    lines.forEach((line, index) => {
      const span = node('tspan', { x, dy: index === 0 ? 0 : size * (opts.leading || 1.35) }, el)
      span.textContent = line
    })
    return el
  }

  function rect(parent, x, y, w, h, options) {
    const opts = options || {}
    return node('rect', {
      x, y, width: w, height: h, rx: opts.rx === undefined ? 14 : opts.rx,
      fill: opts.fill || '#ffffff', stroke: opts.stroke || '#d5e1e8',
      'stroke-width': opts.strokeWidth || 2,
    }, parent)
  }

  function line(parent, x1, y1, x2, y2, options) {
    const opts = options || {}
    return node('line', {
      x1, y1, x2, y2, stroke: opts.color || LINE, 'stroke-width': opts.width || 3,
      'stroke-linecap': 'round', 'marker-end': opts.arrow ? 'url(#arrow)' : '',
      'stroke-dasharray': opts.dash ? '8 8' : '',
    }, parent)
  }

  function makeSvg(container) {
    container.replaceChildren()
    const svg = node('svg', { viewBox: '0 0 ' + WIDTH + ' ' + HEIGHT, role: 'img', 'aria-label': '文章知识图解' }, container)
    const defs = node('defs', {}, svg)
    const marker = node('marker', { id: 'arrow', viewBox: '0 0 10 10', refX: 8, refY: 5, markerWidth: 8, markerHeight: 8, orient: 'auto-start-reverse' }, defs)
    node('path', { d: 'M 0 0 L 10 5 L 0 10 z', fill: LINE }, marker)
    return svg
  }

  function card(parent, x, y, w, h, heading, detail, options) {
    const opts = options || {}
    rect(parent, x, y, w, h, { fill: opts.fill || '#ffffff', stroke: opts.stroke || '#d5e1e8', rx: opts.rx || 14 })
    rect(parent, x, y, 7, h, { fill: opts.color || COLORS[0], stroke: opts.color || COLORS[0], rx: 3, strokeWidth: 0 })
    const centered = opts.center || false
    const tx = centered ? x + w / 2 : x + 22
    const anchor = centered ? 'middle' : 'start'
    const headingSize = opts.headingSize || 22
    text(parent, heading, tx, y + (detail ? 36 : h / 2 + headingSize * 0.34), {
      size: headingSize, width: w - 42, color: opts.headingColor || DARK, weight: 700, anchor,
    })
    if (detail) text(parent, detail, tx, y + (opts.detailY || 72), {
      size: opts.detailSize || 16, width: w - 42, color: MUTED, anchor, leading: 1.28,
    })
  }

  function pill(parent, label, x, y, color) {
    const width = Math.max(74, Array.from(String(label)).length * 18 + 26)
    rect(parent, x - width / 2, y - 19, width, 38, { fill: '#ffffff', stroke: '#dce6eb', rx: 19, strokeWidth: 1 })
    text(parent, label, x, y + 6, { size: 16, width: width - 12, anchor: 'middle', color: color || MUTED })
  }

  function grid(nodes, columns, top, cardWidth, cardHeight, gapY) {
    const rows = Math.ceil(nodes.length / columns)
    const margin = 70
    const gapX = columns === 1 ? 0 : (WIDTH - margin * 2 - cardWidth * columns) / (columns - 1)
    const output = new Map()
    nodes.forEach((item, index) => {
      const row = Math.floor(index / columns)
      const col = index % columns
      const x = margin + col * (cardWidth + gapX)
      const y = top + row * (cardHeight + gapY)
      output.set(item.id, { x, y, w: cardWidth, h: cardHeight, cx: x + cardWidth / 2, cy: y + cardHeight / 2 })
    })
    return output
  }

  function relation(parent, points, items, options) {
    const opts = options || {}
    items.forEach((item) => {
      const a = points.get(item.from)
      const b = points.get(item.to)
      if (!a || !b) return
      line(parent, a.cx, a.cy, b.cx, b.cy, { arrow: Boolean(opts.arrow), dash: opts.dash })
      if (item.label) pill(parent, item.label, (a.cx + b.cx) / 2, (a.cy + b.cy) / 2 - 12)
    })
  }

  function drawGraph(svg, items, edges, options) {
    const opts = options || {}
    const columns = opts.columns || Math.min(3, items.length)
    const w = opts.cardWidth || Math.min(320, (WIDTH - 140 - (columns - 1) * 60) / columns)
    const h = opts.cardHeight || 118
    const points = grid(items, columns, opts.top || 118, w, h, opts.gapY || 72)
    relation(svg, points, edges, { arrow: opts.arrow })
    items.forEach((item, index) => {
      const p = points.get(item.id)
      card(svg, p.x, p.y, p.w, p.h, item.label || item.name, item.detail || (item.members || item.attributes || []).join(' · '), { color: COLORS[index % COLORS.length], headingSize: 20 })
    })
    return points
  }

  function renderUml(svg, d) {
    if (d.variant === 'sequence') {
      const count = d.participants.length
      const gap = (WIDTH - 260) / Math.max(1, count - 1)
      const points = new Map()
      d.participants.forEach((item, i) => {
        const x = 130 + i * gap
        const p = { x: x - 82, y: 50, w: 164, h: 66, cx: x, cy: 83 }
        points.set(item.id, p)
        rect(svg, p.x, p.y, p.w, p.h, { fill: '#f2f7fa' })
        text(svg, item.label, x, 90, { size: 18, width: 148, anchor: 'middle' })
        line(svg, x, 116, x, 612, { dash: true, width: 2 })
      })
      d.messages.forEach((item, i) => {
        const a = points.get(item.from)
        const b = points.get(item.to)
        const y = 166 + i * Math.min(62, 390 / Math.max(1, d.messages.length))
        line(svg, a.cx, y, b.cx, y, { arrow: true, color: COLORS[i % COLORS.length] })
        if (item.label) text(svg, item.label, (a.cx + b.cx) / 2, y - 10, { size: 16, width: 220, anchor: 'middle', color: DARK })
      })
      return
    }
    if (d.variant === 'use_case') {
      const cx = 790
      const cy = 330
      const rx = 470
      const ry = 260
      node('ellipse', { cx, cy, rx, ry, fill: '#f7fafb', stroke: '#d7e3e9', 'stroke-width': 2, 'stroke-dasharray': '9 7' }, svg)
      text(svg, '系统边界', cx, 102, { size: 17, anchor: 'middle', color: MUTED })
      const points = new Map()
      const actorGap = 340 / Math.max(1, d.actors.length - 1)
      d.actors.forEach((item, i) => {
        const x = 115
        const y = d.actors.length === 1 ? 245 : 110 + i * actorGap
        points.set(item.id, { cx: x + 22, cy: y + 66 })
        node('circle', { cx: x + 22, cy: y + 18, r: 15, fill: '#eef4f7', stroke: COLORS[i % COLORS.length], 'stroke-width': 4 }, svg)
        line(svg, x + 22, y + 34, x + 22, y + 72, { color: COLORS[i % COLORS.length], width: 4 })
        line(svg, x + 1, y + 50, x + 43, y + 50, { color: COLORS[i % COLORS.length], width: 4 })
        line(svg, x + 22, y + 72, x + 4, y + 98, { color: COLORS[i % COLORS.length], width: 4 })
        line(svg, x + 22, y + 72, x + 40, y + 98, { color: COLORS[i % COLORS.length], width: 4 })
        text(svg, item.label, x + 22, y + 126, { size: 16, width: 140, anchor: 'middle' })
      })
      const caseGap = 500 / Math.max(1, d.use_cases.length - 1)
      d.use_cases.forEach((item, i) => {
        const x = 580 + (i % 2) * 390
        const y = 155 + Math.floor(i / 2) * Math.min(190, caseGap)
        points.set(item.id, { cx: x + 115, cy: y + 46 })
      })
      relation(svg, points, d.associations)
      d.use_cases.forEach((item, i) => {
        const p = points.get(item.id)
        node('ellipse', { cx: p.cx, cy: p.cy, rx: 116, ry: 43, fill: '#ffffff', stroke: COLORS[i % COLORS.length], 'stroke-width': 3 }, svg)
        text(svg, item.label, p.cx, p.cy + 6, { size: 17, width: 190, anchor: 'middle' })
      })
      return
    }
    drawGraph(svg, d.classes, d.relationships, { columns: Math.min(3, d.classes.length), cardWidth: 350, cardHeight: 140, gapY: 25, top: 62 })
  }

  function renderEntity(svg, d) {
    const columns = Math.min(3, d.entities.length)
    const width = Math.min(350, (WIDTH - 180 - (columns - 1) * 70) / columns)
    const height = 180
    const points = grid(d.entities, columns, 125, width, height, 45)
    relation(svg, points, d.relationships)
    d.entities.forEach((item, index) => {
      const p = points.get(item.id)
      rect(svg, p.x, p.y, p.w, p.h, { fill: '#ffffff' })
      rect(svg, p.x, p.y, p.w, 45, { fill: COLORS[index % COLORS.length], stroke: COLORS[index % COLORS.length], rx: 12, strokeWidth: 1 })
      text(svg, item.label, p.cx, p.y + 29, { size: 20, anchor: 'middle', color: '#ffffff', weight: 700 })
      item.members.forEach((member, i) => text(svg, '• ' + member, p.x + 22, p.y + 70 + i * 18, { size: 15, width: p.w - 36, color: MUTED }))
    })
  }

  function renderChartTable(svg, d) {
    if (d.view === 'table') {
      const x = 100
      const y = 105
      const width = 1240
      const colWidth = width / d.columns.length
      const rowHeight = Math.min(74, 450 / (d.rows.length + 1))
      d.columns.forEach((name, col) => {
        rect(svg, x + col * colWidth, y, colWidth, rowHeight, { fill: COLORS[0], stroke: '#ffffff', rx: 3, strokeWidth: 1 })
        text(svg, name, x + col * colWidth + colWidth / 2, y + rowHeight / 2 + 7, { size: 18, width: colWidth - 20, anchor: 'middle', color: '#ffffff', weight: 700 })
      })
      d.rows.forEach((row, r) => row.forEach((cell, col) => {
        const cy = y + (r + 1) * rowHeight
        rect(svg, x + col * colWidth, cy, colWidth, rowHeight, { fill: r % 2 ? '#f4f8fa' : '#ffffff', rx: 3, strokeWidth: 1 })
        text(svg, cell, x + col * colWidth + colWidth / 2, cy + rowHeight / 2 + 6, { size: 16, width: colWidth - 18, anchor: 'middle', color: DARK })
      }))
      return
    }
    const chartX = 210
    const chartY = 110
    const chartW = 1110
    const chartH = 430
    const max = Math.max(1, ...d.series.flatMap((series) => series.values))
    for (let tick = 0; tick <= 4; tick += 1) {
      const y = chartY + chartH - chartH * tick / 4
      line(svg, chartX, y, chartX + chartW, y, { width: 1, color: '#e3ebef' })
      text(svg, String(Math.round(max * tick / 4 * 100) / 100), chartX - 18, y + 5, { size: 14, width: 80, anchor: 'end', color: MUTED })
    }
    const groupW = chartW / d.categories.length
    const barW = Math.min(64, groupW / (d.series.length + 1))
    d.categories.forEach((category, i) => {
      d.series.forEach((series, j) => {
        const value = series.values[i]
        const height = chartH * value / max
        const x = chartX + i * groupW + groupW / 2 + (j - (d.series.length - 1) / 2) * (barW + 8) - barW / 2
        rect(svg, x, chartY + chartH - height, barW, Math.max(height, 3), { fill: COLORS[j % COLORS.length], stroke: COLORS[j % COLORS.length], rx: 7, strokeWidth: 0 })
        text(svg, String(value), x + barW / 2, chartY + chartH - height - 10, { size: 14, width: 86, anchor: 'middle', color: DARK })
      })
      text(svg, category, chartX + i * groupW + groupW / 2, chartY + chartH + 38, { size: 16, width: groupW - 10, anchor: 'middle' })
    })
    d.series.forEach((series, i) => {
      const x = 470 + i * 230
      rect(svg, x, 595, 18, 18, { fill: COLORS[i % COLORS.length], stroke: COLORS[i % COLORS.length], rx: 4, strokeWidth: 0 })
      text(svg, series.label, x + 29, 611, { size: 16, width: 190, color: MUTED })
    })
  }

  function renderFloorPlan(svg, d) {
    text(svg, '空间关系示意 · 非比例图', 1260, 38, { size: 15, width: 260, anchor: 'end', color: MUTED })
    const cols = Math.min(4, d.rooms.length)
    const points = grid(d.rooms, cols, 130, 250, 120, 85)
    relation(svg, points, d.connections)
    d.rooms.forEach((room, index) => {
      const p = points.get(room.id)
      card(svg, p.x, p.y, p.w, p.h, room.label, room.detail, { color: COLORS[index % COLORS.length], center: true, headingSize: 20 })
    })
  }

  function renderGantt(svg, d) {
    const x0 = 290
    const y0 = 100
    const colW = 1040 / d.periods.length
    const rowH = Math.min(55, 450 / (d.tasks.length + 1))
    d.periods.forEach((period, i) => {
      rect(svg, x0 + i * colW, y0, colW, rowH, { fill: '#eaf1f5', rx: 3, strokeWidth: 1 })
      text(svg, period, x0 + i * colW + colW / 2, y0 + rowH / 2 + 6, { size: 16, width: colW - 12, anchor: 'middle' })
    })
    d.tasks.forEach((task, i) => {
      const y = y0 + (i + 1) * rowH
      text(svg, task.label, x0 - 22, y + rowH / 2 + 6, { size: 16, width: 250, anchor: 'end' })
      d.periods.forEach((period, col) => rect(svg, x0 + col * colW, y, colW, rowH, { fill: i % 2 ? '#f8fafb' : '#ffffff', rx: 2, strokeWidth: 1 }))
      const start = d.periods.indexOf(task.start_period)
      const end = d.periods.indexOf(task.end_period)
      rect(svg, x0 + start * colW + 8, y + 12, (end - start + 1) * colW - 16, rowH - 24, { fill: COLORS[i % COLORS.length], stroke: COLORS[i % COLORS.length], rx: 12, strokeWidth: 0 })
    })
  }

  function renderArchitecture(svg, d) {
    const rowHeight = Math.min(82, 500 / d.layers.length)
    d.layers.forEach((layer, i) => {
      const y = 78 + i * (rowHeight + 12)
      rect(svg, 90, y, 1260, rowHeight, { fill: i % 2 ? '#f8fafb' : '#eef4f7', rx: 12, strokeWidth: 1 })
      text(svg, layer.label, 116, y + 31, { size: 17, width: 210, color: COLORS[i % COLORS.length], weight: 700 })
    })
    const centers = new Map()
    d.layers.forEach((layer, i) => layer.components.forEach((item, j) => {
      const boxW = (970 - (layer.components.length - 1) * 18) / layer.components.length
      centers.set(item.id, { cx: 350 + j * (boxW + 18) + boxW / 2, cy: 78 + i * (rowHeight + 12) + rowHeight / 2 })
    }))
    relation(svg, centers, d.connections, { arrow: true })
    d.layers.forEach((layer, i) => layer.components.forEach((component, j) => {
      const boxW = (970 - (layer.components.length - 1) * 18) / layer.components.length
      const x = 350 + j * (boxW + 18)
      const y = 78 + i * (rowHeight + 12)
      rect(svg, x, y + 11, boxW, rowHeight - 22, { fill: '#ffffff', rx: 9, strokeWidth: 1 })
      text(svg, component.label, x + boxW / 2, y + rowHeight / 2 + 6, { size: 16, width: boxW - 12, anchor: 'middle' })
    }))
  }

  function renderTopology(svg, d) {
    const centerX = WIDTH / 2
    const centerY = HEIGHT / 2
    const radius = Math.min(240, 72 + d.nodes.length * 22)
    const points = new Map()
    d.nodes.forEach((item, index) => {
      const angle = -Math.PI / 2 + index * Math.PI * 2 / d.nodes.length
      points.set(item.id, { cx: centerX + Math.cos(angle) * radius, cy: centerY + Math.sin(angle) * radius })
    })
    relation(svg, points, d.links, { arrow: true })
    d.nodes.forEach((item, index) => {
      const p = points.get(item.id)
      node('circle', { cx: p.cx, cy: p.cy, r: 53, fill: '#ffffff', stroke: COLORS[index % COLORS.length], 'stroke-width': 4 }, svg)
      text(svg, item.label, p.cx, p.cy + 6, { size: 16, width: 96, anchor: 'middle' })
    })
  }

  function renderSwimlane(svg, d) {
    const left = 230
    const right = 1350
    const laneH = 500 / d.lanes.length
    const points = new Map()
    d.lanes.forEach((lane, row) => {
      const y = 72 + row * laneH
      rect(svg, left, y, right - left, laneH - 8, { fill: row % 2 ? '#f8fafb' : '#eef4f7', rx: 10, strokeWidth: 1 })
      text(svg, lane.label, 190, y + laneH / 2 + 5, { size: 17, width: 170, anchor: 'end', color: COLORS[row % COLORS.length], weight: 700 })
      const stepW = Math.min(190, (right - left - 40) / lane.steps.length - 16)
      const gap = (right - left - lane.steps.length * stepW) / (lane.steps.length + 1)
      lane.steps.forEach((step, col) => {
        const x = left + gap + col * (stepW + gap)
        const p = { x, y: y + laneH / 2 - 29, w: stepW, h: 58, cx: x + stepW / 2, cy: y + laneH / 2 }
        points.set(step.id, p)
      })
    })
    relation(svg, points, d.transitions, { arrow: true })
    d.lanes.forEach((lane, row) => lane.steps.forEach((step, col) => {
      const p = points.get(step.id)
      card(svg, p.x, p.y, p.w, p.h, step.label, '', { color: COLORS[(row + col) % COLORS.length], center: true, headingSize: 16 })
    }))
  }

  function renderCircuit(svg, d) {
    const count = d.components.length
    const gap = 1190 / Math.max(1, count - 1)
    const points = new Map()
    d.components.forEach((item, i) => points.set(item.id, { cx: 125 + gap * i, cy: 325 }))
    relation(svg, points, d.wires)
    d.components.forEach((item, i) => {
      const p = points.get(item.id)
      const color = COLORS[i % COLORS.length]
      if (item.kind === 'battery') {
        line(svg, p.cx - 31, p.cy - 22, p.cx - 31, p.cy + 22, { color, width: 5 })
        line(svg, p.cx - 15, p.cy - 35, p.cx - 15, p.cy + 35, { color, width: 5 })
      } else if (item.kind === 'resistor') {
        node('path', { d: 'M ' + (p.cx - 42) + ' ' + p.cy + ' l 12 -18 l 18 36 l 18 -36 l 18 36 l 16 -18', fill: 'none', stroke: color, 'stroke-width': 5 }, svg)
      } else if (item.kind === 'capacitor') {
        line(svg, p.cx - 10, p.cy - 28, p.cx - 10, p.cy + 28, { color, width: 5 })
        line(svg, p.cx + 10, p.cy - 28, p.cx + 10, p.cy + 28, { color, width: 5 })
      } else if (item.kind === 'lamp') {
        node('circle', { cx: p.cx, cy: p.cy, r: 27, fill: '#fff9ea', stroke: color, 'stroke-width': 4 }, svg)
        line(svg, p.cx - 15, p.cy - 15, p.cx + 15, p.cy + 15, { color, width: 3 })
        line(svg, p.cx + 15, p.cy - 15, p.cx - 15, p.cy + 15, { color, width: 3 })
      } else if (item.kind === 'ground') {
        line(svg, p.cx, p.cy - 28, p.cx, p.cy + 5, { color, width: 4 })
        line(svg, p.cx - 24, p.cy + 5, p.cx + 24, p.cy + 5, { color, width: 4 })
        line(svg, p.cx - 15, p.cy + 15, p.cx + 15, p.cy + 15, { color, width: 4 })
        line(svg, p.cx - 7, p.cy + 25, p.cx + 7, p.cy + 25, { color, width: 4 })
      } else {
        node('circle', { cx: p.cx, cy: p.cy, r: 23, fill: '#ffffff', stroke: color, 'stroke-width': 4 }, svg)
        text(svg, ({ inductor: 'L', switch: 'S', diode: 'D', transistor: 'T' })[item.kind] || '·', p.cx, p.cy + 7, { size: 20, anchor: 'middle', color })
      }
      text(svg, item.label, p.cx, p.cy + 76, { size: 16, width: 150, anchor: 'middle' })
    })
  }

  function renderVenn(svg, d) {
    const centers = d.sets.length === 2
      ? [{ x: 610, y: 325 }, { x: 830, y: 325 }]
      : [{ x: 610, y: 290 }, { x: 830, y: 290 }, { x: 720, y: 430 }]
    d.sets.forEach((set, i) => {
      node('circle', { cx: centers[i].x, cy: centers[i].y, r: d.sets.length === 2 ? 226 : 205, fill: COLORS[i % COLORS.length], 'fill-opacity': 0.14, stroke: COLORS[i % COLORS.length], 'stroke-width': 4 }, svg)
      const labelX = centers[i].x + (i === 0 ? -145 : i === 1 ? 145 : 0)
      const labelY = centers[i].y + (i === 2 ? 175 : -160)
      text(svg, set.label, labelX, labelY, { size: 18, width: 200, anchor: 'middle', color: COLORS[i % COLORS.length], weight: 700 })
    })
    const ids = d.sets.map((set) => set.id)
    const positions = new Map()
    if (ids.length === 2) {
      positions.set(ids[0], [500, 330]); positions.set(ids[1], [940, 330]); positions.set(ids.slice().sort().join('|'), [720, 330])
    } else {
      positions.set(ids[0], [515, 280]); positions.set(ids[1], [925, 280]); positions.set(ids[2], [720, 535])
      positions.set([ids[0], ids[1]].sort().join('|'), [720, 260])
      positions.set([ids[0], ids[2]].sort().join('|'), [625, 420])
      positions.set([ids[1], ids[2]].sort().join('|'), [815, 420])
      positions.set(ids.slice().sort().join('|'), [720, 350])
    }
    d.areas.forEach((area) => {
      const p = positions.get(area.sets.slice().sort().join('|'))
      if (!p) return
      const lines = area.items.slice(0, 2)
      lines.forEach((item, i) => text(svg, item, p[0], p[1] + i * 22, { size: 15, width: 180, anchor: 'middle', color: DARK }))
    })
  }

  function renderBpmn(svg, d) {
    const gap = 1170 / Math.max(1, d.nodes.length - 1)
    const points = new Map()
    d.nodes.forEach((item, i) => points.set(item.id, { cx: 130 + i * gap, cy: 315 }))
    relation(svg, points, d.flows, { arrow: true })
    d.nodes.forEach((item, i) => {
      const p = points.get(item.id)
      const color = COLORS[i % COLORS.length]
      if (item.kind === 'gateway') {
        node('path', { d: 'M ' + p.cx + ' 270 L ' + (p.cx + 45) + ' 315 L ' + p.cx + ' 360 L ' + (p.cx - 45) + ' 315 Z', fill: '#fff8e8', stroke: color, 'stroke-width': 4 }, svg)
      } else if (item.kind === 'start' || item.kind === 'end') {
        node('circle', { cx: p.cx, cy: p.cy, r: 35, fill: item.kind === 'start' ? '#e7f4ed' : '#fff0ed', stroke: color, 'stroke-width': item.kind === 'end' ? 7 : 4 }, svg)
      } else {
        rect(svg, p.cx - 66, 278, 132, 74, { fill: '#ffffff', stroke: color, rx: 14, strokeWidth: 3 })
      }
      text(svg, item.label, p.cx, 405, { size: 16, width: 170, anchor: 'middle' })
    })
  }

  function renderPrototype(svg, d) {
    rect(svg, 250, 48, 940, 560, { fill: '#ffffff', stroke: '#bdccd5', rx: 22, strokeWidth: 3 })
    rect(svg, 250, 48, 940, 58, { fill: '#edf3f6', stroke: '#edf3f6', rx: 20, strokeWidth: 1 })
    node('circle', { cx: 286, cy: 77, r: 7, fill: '#d28a48' }, svg)
    node('circle', { cx: 310, cy: 77, r: 7, fill: '#3b8f7a' }, svg)
    node('circle', { cx: 334, cy: 77, r: 7, fill: '#7184bd' }, svg)
    text(svg, d.screen_title, 720, 84, { size: 20, anchor: 'middle', weight: 700 })
    const top = 128
    const sectionGap = 14
    const sectionH = Math.min(130, (455 - sectionGap * (d.sections.length - 1)) / d.sections.length)
    d.sections.forEach((section, i) => {
      const y = top + i * (sectionH + sectionGap)
      text(svg, section.heading, 292, y + 24, { size: 16, width: 240, color: COLORS[i % COLORS.length], weight: 700 })
      const startX = 520
      const itemW = Math.min(560, (620 - section.elements.length * 12) / section.elements.length)
      section.elements.forEach((item, j) => {
        const x = startX + j * (itemW + 12)
        const fill = item.kind === 'button' ? '#e8f1f5' : '#ffffff'
        rect(svg, x, y + 6, itemW, sectionH - 12, { fill, stroke: item.kind === 'button' ? '#a9c4d1' : '#d5e1e8', rx: item.kind === 'button' ? 11 : 6, strokeWidth: 2 })
        text(svg, item.label, x + itemW / 2, y + sectionH / 2 + 5, { size: 15, width: itemW - 12, anchor: 'middle', color: item.kind === 'button' ? COLORS[0] : MUTED })
      })
    })
  }

  function renderFishbone(svg, d) {
    line(svg, 110, 340, 1190, 340, { arrow: true, color: COLORS[0], width: 7 })
    rect(svg, 1195, 286, 205, 108, { fill: '#edf4f7', stroke: COLORS[0], rx: 18, strokeWidth: 3 })
    text(svg, d.effect, 1298, 330, { size: 18, width: 165, anchor: 'middle', weight: 700 })
    d.categories.forEach((category, i) => {
      const x = 230 + i * (900 / Math.max(1, d.categories.length - 1))
      const upper = i % 2 === 0
      const endY = upper ? 135 : 555
      const joinY = upper ? 255 : 425
      line(svg, x, joinY, x + (upper ? 105 : -105), endY, { color: COLORS[i % COLORS.length], width: 4 })
      text(svg, category.label, x + (upper ? 112 : -112), endY + (upper ? -4 : 25), { size: 17, width: 190, anchor: upper ? 'start' : 'end', color: COLORS[i % COLORS.length], weight: 700 })
      category.causes.forEach((cause, j) => {
        const cy = endY + (upper ? 28 : 53) + j * (upper ? 24 : 22)
        text(svg, cause, x + (upper ? 112 : -112), cy, { size: 14, width: 180, anchor: upper ? 'start' : 'end', color: MUTED })
      })
    })
  }

  function renderTree(svg, d, organizational) {
    const root = d.root
    card(svg, 530, 38, 380, 112, root.label, root.detail, { color: COLORS[0], center: true, headingSize: 21, detailY: 62, detailSize: 15 })
    const branchW = Math.min(330, 1160 / d.branches.length - 20)
    const gap = (WIDTH - d.branches.length * branchW) / (d.branches.length + 1)
    const rootP = { cx: WIDTH / 2, cy: 150 }
    const branchPoints = d.branches.map((branch, i) => {
      const x = gap + i * (branchW + gap)
      return { x, y: 210, w: branchW, h: 118, cx: x + branchW / 2, cy: 210 }
    })
    branchPoints.forEach((p) => line(svg, rootP.cx, rootP.cy, p.cx, p.y, { color: LINE, width: 3 }))
    d.branches.forEach((branch, i) => {
      const p = branchPoints[i]
      card(svg, p.x, p.y, p.w, p.h, branch.label, branch.detail, { color: COLORS[i % COLORS.length], center: true, headingSize: 18, detailY: 60, detailSize: 14 })
      const children = branch.children || []
      const childGap = children.length ? (550 - 360) / children.length : 0
      children.forEach((child, j) => {
        const y = 360 + j * childGap
        line(svg, p.cx, p.y + p.h, p.cx, y, { color: '#d8e3e9', width: 2 })
        rect(svg, p.cx - branchW * 0.39, y, branchW * 0.78, 48, { fill: '#ffffff', stroke: '#e0e8ed', rx: 10, strokeWidth: 1 })
        text(svg, child.label, p.cx, y + 30, { size: 15, width: branchW * 0.72, anchor: 'middle' })
      })
    })
    if (organizational) text(svg, '组织结构', 1300, 38, { size: 15, width: 150, anchor: 'end', color: MUTED })
  }

  function renderBracket(svg, d) {
    card(svg, 80, 250, 280, 110, d.root, '', { color: COLORS[0], center: true, headingSize: 22 })
    const groupGap = Math.min(83, 510 / d.groups.length)
    d.groups.forEach((group, i) => {
      const y = 68 + i * groupGap
      const centerY = y + 35
      line(svg, 360, 305, 455, 305, { color: LINE, width: 3 })
      line(svg, 455, centerY, 455, 305, { color: LINE, width: 3 })
      line(svg, 455, centerY, 500, centerY, { color: LINE, width: 3 })
      card(svg, 500, y, 300, 68, group.label, '', { color: COLORS[i % COLORS.length], center: true, headingSize: 18 })
      rect(svg, 850, y, 500, 68, { fill: '#f8fafb', rx: 10, strokeWidth: 1 })
      text(svg, group.items.join('  ·  '), 875, y + 29, { size: 16, width: 450, color: MUTED })
    })
    line(svg, 360, 305, 455, 305, { color: LINE, width: 3 })
  }

  function renderPpt(svg, d) {
    rect(svg, 70, 35, 1300, 110, { fill: '#eaf2f6', stroke: '#d6e3e9', rx: 18, strokeWidth: 1 })
    text(svg, d.key_point, 720, 102, { size: 25, width: 1210, anchor: 'middle', color: DARK, weight: 700 })
    const count = d.sections.length
    const gap = 22
    const cardW = (1300 - gap * (count - 1)) / count
    d.sections.forEach((section, i) => {
      const x = 70 + i * (cardW + gap)
      rect(svg, x, 185, cardW, 350, { fill: '#ffffff', stroke: '#d9e4ea', rx: 18, strokeWidth: 2 })
      rect(svg, x, 185, cardW, 12, { fill: COLORS[i % COLORS.length], stroke: COLORS[i % COLORS.length], rx: 7, strokeWidth: 0 })
      text(svg, String(i + 1).padStart(2, '0'), x + 26, 235, { size: 18, width: 54, color: COLORS[i % COLORS.length], weight: 700 })
      text(svg, section.heading, x + 26, 278, { size: 20, width: cardW - 50, weight: 700 })
      if (section.detail) text(svg, section.detail, x + 26, 324, { size: 15, width: cardW - 50, color: MUTED })
      section.items.forEach((item, j) => {
        node('circle', { cx: x + 34, cy: 390 + j * 44, r: 5, fill: COLORS[i % COLORS.length] }, svg)
        text(svg, item, x + 50, 396 + j * 44, { size: 15, width: cardW - 74, color: MUTED })
      })
    })
    if (d.takeaway) {
      line(svg, 160, 576, 1280, 576, { color: '#d8e3e9', width: 2 })
      text(svg, d.takeaway, 720, 620, { size: 18, width: 1180, anchor: 'middle', color: COLORS[0], weight: 700 })
    }
  }

  const renderers = {
    uml: renderUml,
    er: renderEntity,
    chart_table: renderChartTable,
    floor_plan: renderFloorPlan,
    gantt: renderGantt,
    architecture: renderArchitecture,
    network_topology: renderTopology,
    swimlane: renderSwimlane,
    circuit: renderCircuit,
    venn: renderVenn,
    bpmn: renderBpmn,
    prototype: renderPrototype,
    fishbone: renderFishbone,
    org_chart: (svg, d) => renderTree(svg, d, true),
    bracket: renderBracket,
    ppt_explainer: renderPpt,
  }

  window.renderCustomArticleDiagram = (diagram, container) => {
    if (existingTypes.has(diagram.diagram_type)) return false
    const render = renderers[diagram.diagram_type]
    if (!render) return false
    const svg = makeSvg(container)
    render(svg, diagram)
    return true
  }
})()
