const test = require("node:test")
const assert = require("node:assert/strict")
const fs = require("node:fs")
const path = require("node:path")

const Model = require("../Model.js")
const fixtures = path.join(__dirname, "fixtures")

test("parseSnapshot accepts a live snapshot fixture", () => {
  const raw = fs.readFileSync(path.join(fixtures, "snapshot.json"), "utf8")
  const snapshot = Model.parseSnapshot(raw)
  assert.ok(snapshot)
  assert.equal(snapshot.label, "54° 61°")
  assert.equal(Model.pillText(snapshot), "54° 61°")
  assert.equal(Model.isAlert(snapshot), false)
})

test("parseSnapshot rejects malformed JSON", () => {
  assert.equal(Model.parseSnapshot("{not json"), null)
})

test("pillText and tooltipText format temperatures", () => {
  const snapshot = JSON.parse(fs.readFileSync(path.join(fixtures, "snapshot.json"), "utf8"))
  assert.equal(Model.pillText(snapshot), "54° 61°")
  assert.equal(Model.tooltipText(snapshot), "CPU 54° · GPU 61° · 2100 RPM · mbpfan")
  assert.equal(Model.tempText(54.6), "55°")
  assert.equal(Model.rpmText(2100), "2100 RPM")
  assert.equal(Model.rpmText(0), "stopped")
})

test("configBanner only appears for missing Apple/mbpfan config", () => {
  const missing = JSON.parse(fs.readFileSync(path.join(fixtures, "missing-config.json"), "utf8"))
  assert.match(Model.configBanner(missing), /\/etc\/mbpfan\.conf/)
  const snapshot = JSON.parse(fs.readFileSync(path.join(fixtures, "snapshot.json"), "utf8"))
  assert.equal(Model.configBanner(snapshot), "")
})

test("fanRows and emptyHint describe device state", () => {
  const snapshot = JSON.parse(fs.readFileSync(path.join(fixtures, "snapshot.json"), "utf8"))
  const rows = Model.fanRows(snapshot.cpu)
  assert.equal(rows.length, 1)
  assert.equal(rows[0].status, "2100 RPM  35%")
  assert.equal(Model.emptyHint({ fans: [] }), "No fan RPM exposed for this device")
})

test("deviceMetrics builds usage row only", () => {
  const rows = Model.deviceMetrics({ usage: 42, temp: 54, thermal_percent: 54 }, "CPU")
  assert.equal(rows.length, 1)
  assert.equal(rows[0].label, "CPU")
  assert.equal(rows[0].text, "42%")
})

test("appendTempSample keeps a 60s window", () => {
  const now = 100_000
  let history = Model.appendTempSample([], 50, now - 70_000)
  history = Model.appendTempSample(history, 55, now - 10_000)
  history = Model.appendTempSample(history, 60, now)
  assert.equal(history.length, 2)
  assert.equal(history[0].temp, 55)
  assert.equal(history[1].temp, 60)
})

test("dotBarDots builds stacked columns", () => {
  const now = 60_000
  const interval = 3_000
  const history = []
  for (var i = 0; i < 5; i++) {
    history.push({ t: now - (4 - i) * interval, temp: 40 + i * 5 })
  }
  const dots = Model.dotBarDots(history, now, 200, 32, 60_000, interval, 3, 1)
  assert.ok(dots.length >= 5)
  var slots = {}
  for (var j = 0; j < dots.length; j++) {
    slots[dots[j].slot] = (slots[dots[j].slot] || 0) + 1
  }
  assert.ok(Object.keys(slots).length >= 3)
  assert.equal(Model.tempLevel(82), "hot")
})

test("tempPlotTicks aligns to 3s grid", () => {
  const ticks = Model.tempPlotTicks(200, 60_000, 3_000)
  assert.equal(ticks.length, 21)
  assert.equal(ticks[0].label, "60")
  assert.equal(ticks[ticks.length - 1].label, "0")
  assert.equal(ticks[5].label, "45")
})

test("applyCurveToSnapshot updates config and active preset", () => {
  const snapshot = {
    config: { low_temp: 63, high_temp: 66, max_temp: 86 },
    presets: [
      { id: "quiet", label: "Quiet", active: false },
      { id: "balanced", label: "Balanced", active: true },
      { id: "cool", label: "Cool", active: false },
    ],
  }
  const updated = Model.applyCurveToSnapshot(snapshot, { low_temp: 70, high_temp: 78, max_temp: 90 }, "quiet")
  assert.equal(updated.config.low_temp, 70)
  assert.equal(updated.presets.find((p) => p.id === "quiet").active, true)
  assert.equal(updated.presets.find((p) => p.id === "balanced").active, false)
})

test("followStatusText and applyFollowToSnapshot", () => {
  const snapshot = {
    follow: { running: false },
    config: { low_temp: 63, high_temp: 66, max_temp: 86 },
  }
  assert.equal(Model.followStatusText(snapshot), "")
  const running = Model.applyFollowToSnapshot(snapshot, { running: true, percent: 45, temp: 54.2 })
  assert.equal(running.follow.running, true)
  assert.equal(running.follow.percent, 45)
  assert.match(Model.followStatusText(running), /45% duty/)
  assert.match(Model.followStatusText(running), /CPU 54°/)
})
