// Data layer for the WindCore panel. Pure functions so the same
// file loads in Quickshell and in node --test.

function clean(value, max) {
  var s = String(value === undefined || value === null ? "" : value)
  s = s.replace(/[<>]/g, "").replace(/[\x00-\x1f\x7f]/g, "")
  var cap = max || 64
  return s.length > cap ? s.slice(0, cap) : s
}

function parseSnapshot(raw) {
  var data
  try { data = JSON.parse(String(raw || "")) } catch (e) { return null }
  if (!data || typeof data !== "object") return null
  if (data.ok === false && !data.cpu && !data.gpu) return null
  return data
}

function tempText(value) {
  if (value === undefined || value === null || value === "") return "—"
  var n = Number(value)
  if (isNaN(n)) return "—"
  return Math.round(n) + "°"
}

function rpmText(value) {
  if (value === undefined || value === null) return "—"
  var n = Number(value)
  if (isNaN(n) || n <= 0) return "stopped"
  if (n >= 1000) return Math.round(n) + " RPM"
  return n + " RPM"
}

function percentText(value) {
  if (value === undefined || value === null || isNaN(Number(value))) return "—"
  return Math.round(Number(value)) + "%"
}

function clamp01(value) {
  var n = Number(value)
  if (isNaN(n)) return 0
  return Math.max(0, Math.min(1, n / 100))
}

var TEMP_HISTORY_WINDOW_MS = 60000
var TEMP_SAMPLE_INTERVAL_MS = 3000

function appendTempSample(history, temp, nowMs, windowMs) {
  windowMs = windowMs || TEMP_HISTORY_WINDOW_MS
  if (temp === undefined || temp === null || isNaN(Number(temp))) return history || []
  var next = (history || []).slice()
  next.push({ t: nowMs, temp: Number(temp) })
  var cutoff = nowMs - windowMs
  var out = []
  for (var i = 0; i < next.length; i++) {
    if (next[i].t >= cutoff) out.push(next[i])
  }
  return out
}

function tempPlotRange(history) {
  if (!history || !history.length) return { min: 30, max: 90 }
  var min = history[0].temp
  var max = history[0].temp
  for (var i = 1; i < history.length; i++) {
    min = Math.min(min, history[i].temp)
    max = Math.max(max, history[i].temp)
  }
  if (max - min < 8) {
    min -= 4
    max += 4
  } else {
    var pad = Math.max(2, (max - min) * 0.12)
    min -= pad
    max += pad
  }
  return { min: min, max: max }
}

function tempLevel(temp) {
  if (temp >= 80) return "hot"
  if (temp >= 70) return "warm"
  if (temp >= 55) return "normal"
  return "cool"
}

function tempPlotTicks(plotWidth, windowMs, intervalMs) {
  windowMs = windowMs || TEMP_HISTORY_WINDOW_MS
  intervalMs = intervalMs || TEMP_SAMPLE_INTERVAL_MS
  plotWidth = plotWidth || 200
  var slots = Math.max(1, Math.floor(windowMs / intervalMs))
  var colWidth = plotWidth / slots
  var ticks = []
  for (var i = 0; i <= slots; i++) {
    var ageSec = Math.round((windowMs - i * intervalMs) / 1000)
    ticks.push({
      x: i * colWidth,
      major: i % 5 === 0,
      label: i % 5 === 0 ? String(ageSec) : ""
    })
  }
  return ticks
}

function dotBarDots(history, nowMs, plotWidth, plotHeight, windowMs, intervalMs, dotSize, dotGap) {
  windowMs = windowMs || TEMP_HISTORY_WINDOW_MS
  intervalMs = intervalMs || TEMP_SAMPLE_INTERVAL_MS
  dotSize = dotSize || 3
  dotGap = dotGap || 1
  plotWidth = plotWidth || 200
  plotHeight = plotHeight || 32
  var slots = Math.max(1, Math.floor(windowMs / intervalMs))
  var start = nowMs - windowMs
  var buckets = []
  for (var s = 0; s < slots; s++) buckets.push(null)

  if (history && history.length) {
    for (var i = 0; i < history.length; i++) {
      var p = history[i]
      if (p.t < start) continue
      var slot = Math.floor((p.t - start) / intervalMs)
      if (slot < 0 || slot >= slots) continue
      if (!buckets[slot] || p.t >= buckets[slot].t) buckets[slot] = p
    }
  }

  var filled = []
  for (var b = 0; b < buckets.length; b++) {
    if (buckets[b]) filled.push(buckets[b])
  }
  if (!filled.length) return []

  // Carry the last sample forward so each 3s column stays filled (matches panel sampling).
  var carry = null
  for (var c = 0; c < slots; c++) {
    if (buckets[c]) carry = buckets[c]
    else if (carry) buckets[c] = { t: start + c * intervalMs, temp: carry.temp, carried: true }
  }

  var range = tempPlotRange(filled)
  var span = Math.max(1, range.max - range.min)
  var bottomPad = 2
  var maxRows = Math.max(4, Math.floor((plotHeight - bottomPad) / (dotSize + dotGap)))
  var colWidth = plotWidth / slots
  var dots = []

  for (var col = 0; col < slots; col++) {
    var sample = buckets[col]
    if (!sample) continue
    var rows = Math.max(1, Math.round(((sample.temp - range.min) / span) * maxRows))
    var cx = (col + 0.5) * colWidth
    for (var row = 0; row < rows; row++) {
      dots.push({
        x: cx - dotSize / 2,
        y: plotHeight - bottomPad - (row + 1) * (dotSize + dotGap),
        temp: sample.temp,
        level: tempLevel(sample.temp),
        slot: col,
        latest: col === slots - 1,
        carried: sample.carried === true
      })
    }
  }
  return dots
}

function deviceMetrics(device, usageLabel) {
  if (!device) return []
  var label = usageLabel || "CPU"
  var rows = []
  var usage = device.usage
  if (usage !== undefined && usage !== null && !isNaN(Number(usage))) {
    rows.push({
      id: "usage",
      label: label,
      ratio: clamp01(usage),
      text: percentText(usage),
      hot: Number(usage) >= 90
    })
  }
  return rows
}

function pillText(snapshot) {
  if (!snapshot) return "WC …"
  if (snapshot.label) return clean(snapshot.label, 16)
  var cpu = snapshot.cpu && snapshot.cpu.temp
  var gpu = snapshot.gpu && snapshot.gpu.temp
  if (cpu == null && gpu == null) return "WC …"
  if (cpu != null && gpu != null) return Math.round(cpu) + "° " + Math.round(gpu) + "°"
  return Math.round(cpu != null ? cpu : gpu) + "°"
}

function tooltipText(snapshot) {
  if (!snapshot) return "Fan control"
  if (snapshot.tooltip) return clean(snapshot.tooltip, 80)
  return "CPU and GPU fans"
}

function isAlert(snapshot) {
  return !!(snapshot && snapshot.alert)
}

function configBanner(snapshot) {
  if (!snapshot || !snapshot.backend) return ""
  var name = String(snapshot.backend.name || "")
  if (name !== "mbpfan" && name !== "applesmc") return ""
  var missing = snapshot.backend.config_missing || (snapshot.config && snapshot.config.system_missing)
  if (!missing) return ""
  return "Could not read /etc/mbpfan.conf. Using the bundled curve until you install it."
}

function backendTitle(snapshot) {
  if (!snapshot || !snapshot.backend) return "Detecting fans"
  var name = String(snapshot.backend.name || "")
  if (name === "mbpfan") return snapshot.backend.running ? "mbpfan running" : "mbpfan installed"
  if (name === "t2fanrd") return snapshot.backend.running ? "t2fanrd running" : "t2fanrd installed"
  if (name === "applesmc") return "Apple SMC fans"
  if (name === "hwmon") return "Kernel hwmon PWM"
  if (name === "demo") return "Demo sensors"
  if (name === "monitor") return "Sensors only"
  return clean(name, 32) || "Fan control"
}

function deviceTitle(device, fallback) {
  if (!device || !device.name) return fallback
  return clean(device.name, 40)
}

function fanStatus(fan) {
  if (!fan) return "no reading"
  var rpm = fan.rpm
  var pct = fan.pwm_percent
  var bits = []
  var idle = (rpm != null && Number(rpm) <= 0) || (pct != null && Number(pct) <= 0)
  if (rpm != null && !isNaN(Number(rpm)) && Number(rpm) > 0) bits.push(rpmText(rpm))
  else if (idle) bits.push("idle")
  if (pct != null && !isNaN(Number(pct))) bits.push(percentText(pct))
  if (!bits.length) return "no reading"
  return bits.join("  ")
}

function fanRows(device) {
  var fans = device && device.fans ? device.fans : []
  var out = []
  for (var i = 0; i < fans.length; i++) {
    out.push({
      id: clean(fans[i].id || ("fan" + i), 40),
      label: clean(fans[i].label || ("Fan " + (i + 1)), 24),
      rpm: rpmText(fans[i].rpm),
      percent: percentText(fans[i].pwm_percent),
      status: fanStatus(fans[i])
    })
  }
  return out
}

function emptyHint(device) {
  if (!device) return ""
  if (device.fans && device.fans.length) return ""
  return clean(device.empty || "No fan RPM exposed for this device", 180)
}

function idleHint(device) {
  if (!device) return ""
  var n = Number(device.idle_fans || 0)
  if (!n) return ""
  return n === 1 ? "1 idle header" : (n + " idle headers")
}

function notes(snapshot) {
  if (!snapshot || !snapshot.backend || !snapshot.backend.notes) return []
  var out = []
  for (var i = 0; i < snapshot.backend.notes.length; i++) {
    out.push(clean(snapshot.backend.notes[i], 180))
  }
  return out
}

function applyCurveToSnapshot(snapshot, curve, presetId) {
  if (!snapshot || !curve) return snapshot
  var next = JSON.parse(JSON.stringify(snapshot))
  next.config = Object.assign({}, next.config || {}, curve)
  if (presetId && next.presets) {
    for (var i = 0; i < next.presets.length; i++) {
      next.presets[i].active = next.presets[i].id === presetId
    }
  }
  return next
}

function applyFollowToSnapshot(snapshot, follow) {
  if (!snapshot || !follow || follow.running === undefined) return snapshot
  var next = JSON.parse(JSON.stringify(snapshot))
  next.follow = Object.assign({}, next.follow || {}, follow)
  return next
}

function followStatusText(snapshot) {
  if (!snapshot || !snapshot.follow || !snapshot.follow.running) return ""
  var f = snapshot.follow
  var bits = ["Following saved curve"]
  if (f.percent != null && !isNaN(Number(f.percent)))
    bits.push(Math.round(Number(f.percent)) + "% duty")
  if (f.temp != null && !isNaN(Number(f.temp)))
    bits.push("CPU " + Math.round(Number(f.temp)) + "°")
  return bits.join(" · ")
}

if (typeof module !== "undefined") {
  module.exports = {
    clean: clean,
    parseSnapshot: parseSnapshot,
    tempText: tempText,
    rpmText: rpmText,
    percentText: percentText,
    clamp01: clamp01,
    appendTempSample: appendTempSample,
    tempPlotRange: tempPlotRange,
    tempPlotTicks: tempPlotTicks,
    tempLevel: tempLevel,
    dotBarDots: dotBarDots,
    TEMP_HISTORY_WINDOW_MS: TEMP_HISTORY_WINDOW_MS,
    TEMP_SAMPLE_INTERVAL_MS: TEMP_SAMPLE_INTERVAL_MS,
    deviceMetrics: deviceMetrics,
    pillText: pillText,
    tooltipText: tooltipText,
    isAlert: isAlert,
    configBanner: configBanner,
    backendTitle: backendTitle,
    deviceTitle: deviceTitle,
    fanRows: fanRows,
    fanStatus: fanStatus,
    emptyHint: emptyHint,
    idleHint: idleHint,
    notes: notes,
    applyCurveToSnapshot: applyCurveToSnapshot,
    applyFollowToSnapshot: applyFollowToSnapshot,
    followStatusText: followStatusText
  }
}
