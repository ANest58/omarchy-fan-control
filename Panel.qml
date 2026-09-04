import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui
import "Model.js" as Model

// WindCore panel. Snapshot stream from scripts/fanctl.py; apply/install
// go through a second process so the live feed is never killed.
Panel {
  id: root
  moduleName: "io.github.anesturi.fan-control"
  ipcTarget: "io.github.anesturi.fan-control"
  manageIpc: false

  property var anchorItem: null
  property bool openedFromHotkey: false
  property var hostWidget: null
  readonly property var barIdentity: hostWidget || root

  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color dim: Qt.darker(foreground, 1.45)
  readonly property color track: Style.selectedFillFor ? Style.selectedFillFor(foreground, Color.accent) : Qt.rgba(foreground.r, foreground.g, foreground.b, 0.12)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family

  readonly property int refreshSeconds: {
    var n = Number(setting("refreshSeconds", 2)) || 2
    return Math.max(1, Math.min(10, n))
  }

  property var snapshot: null
  property string actionStatus: ""
  property bool actionBusy: false
  property var cpuTempHistory: []
  property var gpuTempHistory: []

  readonly property int tempHistoryWindowMs: Model.TEMP_HISTORY_WINDOW_MS
  readonly property int tempSampleIntervalMs: Model.TEMP_SAMPLE_INTERVAL_MS

  readonly property bool loaded: snapshot !== null
  readonly property bool isAlert: Model.isAlert(snapshot)
  readonly property string label: Model.pillText(snapshot)
  readonly property string tooltip: loaded ? Model.tooltipText(snapshot) : "Reading fans…"
  readonly property string banner: Model.configBanner(snapshot)
  readonly property var cpu: snapshot && snapshot.cpu ? snapshot.cpu : null
  readonly property var gpu: snapshot && snapshot.gpu ? snapshot.gpu : null
  readonly property var presets: snapshot && snapshot.presets ? snapshot.presets : []
  readonly property var noteList: Model.notes(snapshot)
  readonly property bool followRunning: !!(snapshot && snapshot.follow && snapshot.follow.running)
  readonly property string followStatusText: Model.followStatusText(snapshot)
  readonly property bool needsGrant: !!(
    snapshot && snapshot.backend
    && snapshot.backend.name === "hwmon"
    && snapshot.backend.can_control
    && snapshot.backend.passwordless === false
  )

  readonly property string scriptPath: decodeURIComponent(Qt.resolvedUrl("scripts/fanctl.py").toString().replace(/^file:\/\//, ""))

  function open() {
    openedFromHotkey = false
    root.controller.show()
  }

  function openFromHotkey() {
    openedFromHotkey = true
    root.controller.show()
  }

  function close() {
    root.controller.hide()
  }

  function toggle() {
    if (root.opened) root.close()
    else root.openFromHotkey()
  }

  function switchPanel(direction) {
    if (root.bar && typeof root.bar.switchPanelFrom === "function")
      return root.bar.switchPanelFrom(root.barIdentity, direction)
    return false
  }

  function ingest(line) {
    var parsed = Model.parseSnapshot(line)
    if (parsed) root.snapshot = parsed
  }

  function refresh() {
    if (!oneshot.running) oneshot.running = true
  }

  function runAction(args) {
    if (actionProc.running) return
    root.actionBusy = true
    root.actionStatus = ""
    actionProc.command = ["python3", root.scriptPath].concat(args)
    actionProc.running = true
  }

  function installConfig() {
    runAction(["install-config"])
  }

  function grantAccess() {
    runAction(["grant-access"])
  }

  function applyPreset(id) {
    // --user-only: never pkexec for /etc/mbpfan.conf on desktop; PWM uses direct write or one grant.
    runAction(["apply", "--preset", id, "--user-only"])
  }

  function toggleFollow() {
    if (root.snapshot && root.snapshot.follow && root.snapshot.follow.running)
      runAction(["follow", "stop"])
    else
      runAction(["follow", "start"])
  }

  function sampleTemps() {
    var now = Date.now()
    if (root.cpu && root.cpu.temp !== undefined && root.cpu.temp !== null)
      root.cpuTempHistory = Model.appendTempSample(root.cpuTempHistory, root.cpu.temp, now, tempHistoryWindowMs)
    if (root.gpu && root.gpu.temp !== undefined && root.gpu.temp !== null)
      root.gpuTempHistory = Model.appendTempSample(root.gpuTempHistory, root.gpu.temp, now, tempHistoryWindowMs)
  }

  Timer {
    id: tempSampler
    interval: root.tempSampleIntervalMs
    repeat: true
    running: true
    triggeredOnStart: true
    onTriggered: root.sampleTemps()
  }

  Process {
    id: stream
    command: ["python3", root.scriptPath, "snapshot", "--interval", String(root.refreshSeconds)]
    running: true
    stdout: SplitParser {
      onRead: function(line) { root.ingest(line) }
    }
  }

  Process {
    id: oneshot
    command: ["python3", root.scriptPath, "snapshot"]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.ingest(text)
    }
  }

  Process {
    id: actionProc
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        var parsed = Model.parseSnapshot(text)
        root.actionBusy = false
        if (parsed && parsed.ok) {
          if (parsed.curve && root.snapshot) {
            root.snapshot = Model.applyCurveToSnapshot(root.snapshot, parsed.curve, parsed.preset)
          }
          if (parsed.running !== undefined && root.snapshot) {
            root.snapshot = Model.applyFollowToSnapshot(root.snapshot, parsed)
          }
          var pwm = parsed.pwm
          if (parsed.udev || parsed.chmod || parsed.count != null && parsed.polkit !== undefined)
            root.actionStatus = "Passwordless fan control enabled"
          else if (parsed.running === true)
            root.actionStatus = parsed.percent != null
              ? "Following curve · " + Math.round(Number(parsed.percent)) + "% duty"
              : "Following temperature curve"
          else if (parsed.running === false && parsed.pid === undefined && !parsed.pwm && !parsed.path)
            root.actionStatus = "Curve follow stopped"
          else if (pwm && pwm.attempted && pwm.ok && pwm.percent != null)
            root.actionStatus = "Fans set to " + Math.round(Number(pwm.percent)) + "%"
          else if (parsed.warning)
            root.actionStatus = Model.clean(parsed.warning, 120)
          else if (parsed.path || parsed.system_path || parsed.user_path)
            root.actionStatus = "Wrote " + Model.clean(parsed.path || parsed.system_path || parsed.user_path, 64)
          else
            root.actionStatus = "Curve saved"
          root.refresh()
        } else if (parsed && parsed.error) {
          root.actionStatus = Model.clean(parsed.error, 120)
        } else {
          root.actionStatus = text ? Model.clean(text, 120) : "Done"
          root.refresh()
        }
      }
    }
    stderr: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        if (text && text.length) root.actionStatus = Model.clean(text, 120)
      }
    }
    onExited: function(code) {
      root.actionBusy = false
      if (code !== 0 && !root.actionStatus) root.actionStatus = "Could not apply fan curve"
    }
  }

  IpcHandler {
    target: root.ipcTarget
    function open(): void { root.openFromHotkey() }
    function close(): void { root.close() }
    function show(): void { root.openFromHotkey() }
    function hide(): void { root.close() }
    function toggle(): void { root.toggle() }
    function refresh(): void {
      if (root.hostWidget && typeof root.hostWidget.broadcast === "function")
        root.hostWidget.broadcast("refresh")
      else root.refresh()
    }
  }

  KeyboardPanel {
    id: panel
    anchorItem: root.anchorItem
    owner: root.barIdentity
    bar: root.bar
    open: root.opened
    centerOnBar: true
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(420))
    contentHeight: panel.fittedContentHeight(contentColumn.implicitHeight)

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onActivateRequested: root.refresh()
      onTextKey: function(t) {
        if (t === "i" || t === "I") root.installConfig()
        if (t === "1") root.applyPreset("quiet")
        if (t === "2") root.applyPreset("balanced")
        if (t === "3") root.applyPreset("cool")
        if (t === "f" || t === "F") root.toggleFollow()
      }

      Flickable {
        anchors.fill: parent
        contentWidth: width
        contentHeight: contentColumn.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        interactive: contentHeight > height

        Column {
          id: contentColumn
          width: parent.width
          spacing: Style.space(12)

          Item {
            width: parent.width
            implicitHeight: Math.max(heroTemp.implicitHeight, heroLabels.implicitHeight)

            Text {
              id: heroTemp
              text: root.snapshot && root.snapshot.hottest != null ? Model.tempText(root.snapshot.hottest) : "—"
              textFormat: Text.PlainText
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.displayLarge
              font.bold: true
              anchors.right: parent.right
              anchors.rightMargin: Style.space(16)
              anchors.verticalCenter: parent.verticalCenter
            }

            Column {
              id: heroLabels
              anchors.left: parent.left
              anchors.leftMargin: Style.space(16)
              anchors.right: heroTemp.left
              anchors.rightMargin: Style.space(10)
              anchors.verticalCenter: parent.verticalCenter
              spacing: Style.space(2)

              Text {
                text: "WIND"
                textFormat: Text.PlainText
                width: parent.width
                elide: Text.ElideRight
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.title
                font.bold: true
                font.letterSpacing: 1
              }

              Text {
                text: Model.backendTitle(root.snapshot)
                textFormat: Text.PlainText
                width: parent.width
                elide: Text.ElideRight
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                font.bold: true
                font.letterSpacing: 1.1
              }
            }
          }

          Column {
            visible: root.banner !== ""
            width: parent.width
            spacing: Style.space(8)

            PanelSeparator { foreground: root.foreground }

            Text {
              text: root.banner
              textFormat: Text.PlainText
              wrapMode: Text.WordWrap
              width: parent.width
              leftPadding: Style.space(16)
              rightPadding: Style.space(16)
              color: root.foreground
              font.family: root.fontFamily
              font.pixelSize: Style.font.bodySmall
            }

            Button {
              anchors.left: parent.left
              anchors.leftMargin: Style.space(16)
              text: root.actionBusy ? "Installing…" : "Install /etc/mbpfan.conf"
              enabled: !root.actionBusy
              foreground: root.foreground
              fontFamily: root.fontFamily
              bordered: true
              onClicked: root.installConfig()
            }
          }

          PanelSeparator { foreground: root.foreground }

          DeviceBlock {
            title: "CPU"
            device: root.cpu
            usageLabel: "CPU"
            tempHistory: root.cpuTempHistory
            width: parent.width
          }

          DeviceBlock {
            title: "GPU"
            device: root.gpu
            usageLabel: "GPU"
            tempHistory: root.gpuTempHistory
            width: parent.width
          }

          PanelSeparator { foreground: root.foreground }

          Column {
            width: parent.width
            spacing: Style.space(8)

            PanelSectionHeader {
              text: "CURVE"
              leftPadding: Style.space(16)
              foreground: root.foreground
              fontFamily: root.fontFamily
            }

            Text {
              visible: root.snapshot && root.snapshot.config
              text: root.snapshot && root.snapshot.config
                ? ("Low " + root.snapshot.config.low_temp + "°  High " + root.snapshot.config.high_temp + "°  Max " + root.snapshot.config.max_temp + "°")
                : ""
              textFormat: Text.PlainText
              leftPadding: Style.space(16)
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.bodySmall
            }

            Row {
              anchors.left: parent.left
              anchors.leftMargin: Style.space(16)
              anchors.right: parent.right
              anchors.rightMargin: Style.space(16)
              spacing: Style.space(6)

              Repeater {
                model: root.presets.length ? root.presets : [
                  { id: "quiet", label: "Quiet", active: false },
                  { id: "balanced", label: "Balanced", active: false },
                  { id: "cool", label: "Cool", active: false }
                ]

                Button {
                  required property var modelData
                  width: (parent.width - parent.spacing * 2) / 3
                  text: String(modelData.label || modelData.id)
                  foreground: root.foreground
                  fontFamily: root.fontFamily
                  bordered: true
                  active: modelData.active === true
                  enabled: !root.actionBusy
                  onClicked: root.applyPreset(String(modelData.id))
                }
              }
            }

            Button {
              anchors.left: parent.left
              anchors.leftMargin: Style.space(16)
              text: root.actionBusy
                ? (root.followRunning ? "Stopping…" : "Starting…")
                : (root.followRunning ? "Stop follow curve" : "Follow curve")
              enabled: !root.actionBusy && root.snapshot && root.snapshot.backend
                && root.snapshot.backend.can_control
              foreground: root.foreground
              fontFamily: root.fontFamily
              bordered: true
              active: root.followRunning
              onClicked: root.toggleFollow()
            }

            Text {
              visible: root.followStatusText !== ""
              text: root.followStatusText
              textFormat: Text.PlainText
              wrapMode: Text.WordWrap
              width: parent.width
              leftPadding: Style.space(16)
              rightPadding: Style.space(16)
              color: root.followRunning ? root.foreground : root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }

            Button {
              visible: root.needsGrant
              anchors.left: parent.left
              anchors.leftMargin: Style.space(16)
              text: root.actionBusy ? "Unlocking…" : "Allow passwordless control"
              enabled: !root.actionBusy
              foreground: root.foreground
              fontFamily: root.fontFamily
              bordered: true
              onClicked: root.grantAccess()
            }

            Text {
              visible: root.actionStatus !== ""
              text: root.actionStatus
              textFormat: Text.PlainText
              wrapMode: Text.WordWrap
              width: parent.width
              leftPadding: Style.space(16)
              rightPadding: Style.space(16)
              color: root.dim
              font.family: root.fontFamily
              font.pixelSize: Style.font.caption
            }
          }

          Column {
            visible: root.noteList.length > 0
            width: parent.width
            spacing: Style.space(4)

            Repeater {
              model: root.noteList
              Text {
                required property var modelData
                text: modelData
                textFormat: Text.PlainText
                wrapMode: Text.WordWrap
                width: contentColumn.width
                leftPadding: Style.space(16)
                rightPadding: Style.space(16)
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
              }
            }
          }

          Item { width: 1; height: Style.space(4) }
        }
      }
    }
  }

  component DeviceBlock: Column {
    property string title: ""
    property string usageLabel: "CPU"
    property var device: null
    property var tempHistory: []

    readonly property bool present: device && (
      device.temp != null
      || device.usage != null
      || device.thermal_percent != null
      || (device.fans && device.fans.length)
    )
    spacing: Style.space(4)
    visible: present || !root.loaded

    Item {
      width: parent.width
      height: Style.space(22)

      Text {
        anchors.left: parent.left
        anchors.leftMargin: Style.space(16)
        anchors.verticalCenter: parent.verticalCenter
        text: title + (device && device.name ? "  " + Model.clean(device.name, 28) : "")
        textFormat: Text.PlainText
        width: parent.width * 0.7
        elide: Text.ElideRight
        color: root.foreground
        font.family: root.fontFamily
        font.pixelSize: Style.font.body
      }

      Text {
        anchors.right: parent.right
        anchors.rightMargin: Style.space(16)
        anchors.verticalCenter: parent.verticalCenter
        text: Model.tempText(device ? device.temp : null)
        textFormat: Text.PlainText
        color: root.foreground
        font.family: root.fontFamily
        font.pixelSize: Style.font.body
        font.bold: true
      }
    }

    MetricBar {
      visible: device && device.usage !== undefined && device.usage !== null
      width: parent.width
      label: usageLabel
      ratio: Model.clamp01(device ? device.usage : 0)
      valueText: Model.percentText(device ? device.usage : null)
      hot: device && Number(device.usage) >= 90
    }

    TempDotBar {
      visible: device && device.temp !== undefined && device.temp !== null
      width: parent.width
      history: tempHistory
      currentTemp: device ? device.temp : null
      windowMs: root.tempHistoryWindowMs
      sampleMs: root.tempSampleIntervalMs
    }

    Repeater {
      model: Model.fanRows(device)

      Item {
        required property var modelData
        width: parent.width
        height: Style.space(20)

        Text {
          anchors.left: parent.left
          anchors.leftMargin: Style.space(16)
          anchors.verticalCenter: parent.verticalCenter
          text: modelData.label
          textFormat: Text.PlainText
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.bodySmall
        }

        Text {
          anchors.right: parent.right
          anchors.rightMargin: Style.space(16)
          anchors.verticalCenter: parent.verticalCenter
          text: modelData.status
          textFormat: Text.PlainText
          color: root.dim
          font.family: root.fontFamily
          font.pixelSize: Style.font.bodySmall
        }
      }
    }

    Text {
      visible: present && !(device.fans && device.fans.length)
      text: Model.emptyHint(device)
      textFormat: Text.PlainText
      leftPadding: Style.space(16)
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
    }

    Text {
      visible: device && Number(device.idle_fans || 0) > 0
      text: Model.idleHint(device)
      textFormat: Text.PlainText
      leftPadding: Style.space(16)
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
    }
  }

  component MetricBar: Item {
    property string label: ""
    property real ratio: 0
    property string valueText: ""
    property bool hot: false

    implicitHeight: Style.space(20)

    readonly property real barLeft: Style.space(52)
    readonly property real barRight: Style.space(44)

    Text {
      text: label
      textFormat: Text.PlainText
      anchors.left: parent.left
      anchors.leftMargin: Style.space(16)
      anchors.verticalCenter: parent.verticalCenter
      color: root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      font.letterSpacing: 0.8
    }

    Text {
      text: valueText
      textFormat: Text.PlainText
      anchors.right: parent.right
      anchors.rightMargin: Style.space(16)
      anchors.verticalCenter: parent.verticalCenter
      color: hot ? Color.accent : root.dim
      font.family: root.fontFamily
      font.pixelSize: Style.font.caption
      font.bold: hot
    }

    Item {
      anchors.left: parent.left
      anchors.leftMargin: barLeft
      anchors.right: parent.right
      anchors.rightMargin: barRight
      anchors.verticalCenter: parent.verticalCenter
      height: Math.max(Style.space(5), Math.round(Style.spacing.controlHeight * 0.16))

      Rectangle {
        id: metricTrack
        anchors.fill: parent
        radius: height / 2
        color: root.track
      }

      Rectangle {
        anchors.left: metricTrack.left
        anchors.verticalCenter: metricTrack.verticalCenter
        height: metricTrack.height
        radius: metricTrack.radius
        width: metricTrack.width * Math.max(0, Math.min(1, ratio))
        color: hot ? Color.accent : root.foreground

        Behavior on width {
          NumberAnimation { duration: 180; easing.type: Easing.OutCubic }
        }
      }
    }
  }

  component TempDotBar: Item {
    property var history: []
    property var currentTemp: null
    property int windowMs: Model.TEMP_HISTORY_WINDOW_MS
    property int sampleMs: Model.TEMP_SAMPLE_INTERVAL_MS

    readonly property real plotLeft: Style.space(52)
    readonly property real plotRight: Style.space(16)
    readonly property real dotSize: 3
    readonly property real dotGap: 1
    property real nowMs: Date.now()

    implicitHeight: headerRow.height + Style.space(2) + plotArea.height + tickRow.height

    function levelColor(level) {
      if (level === "hot") return Color.accent
      if (level === "warm") return Qt.lighter(Color.accent, 1.35)
      if (level === "normal") return root.foreground
      return root.dim
    }

    Timer {
      interval: sampleMs
      repeat: true
      running: true
      triggeredOnStart: true
      onTriggered: nowMs = Date.now()
    }

    Item {
      id: headerRow
      width: parent.width
      height: Style.space(18)

      Text {
        text: "TEMP"
        textFormat: Text.PlainText
        anchors.left: parent.left
        anchors.leftMargin: Style.space(16)
        anchors.verticalCenter: parent.verticalCenter
        color: root.dim
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        font.letterSpacing: 0.8
      }

      Text {
        text: "60s"
        textFormat: Text.PlainText
        anchors.left: parent.left
        anchors.leftMargin: Style.space(52)
        anchors.verticalCenter: parent.verticalCenter
        color: Qt.rgba(root.dim.r, root.dim.g, root.dim.b, 0.75)
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
      }

      Text {
        text: Model.tempText(currentTemp)
        textFormat: Text.PlainText
        anchors.right: parent.right
        anchors.rightMargin: Style.space(16)
        anchors.verticalCenter: parent.verticalCenter
        color: currentTemp !== null && Number(currentTemp) >= 80 ? Color.accent : root.foreground
        font.family: root.fontFamily
        font.pixelSize: Style.font.caption
        font.bold: currentTemp !== null && Number(currentTemp) >= 80
      }
    }

    Item {
      id: plotArea
      anchors.top: headerRow.bottom
      anchors.topMargin: Style.space(2)
      anchors.left: parent.left
      anchors.leftMargin: plotLeft
      anchors.right: parent.right
      anchors.rightMargin: plotRight
      height: Style.space(32)

      readonly property var dots: Model.dotBarDots(
        history,
        nowMs,
        width,
        height,
        windowMs,
        sampleMs,
        dotSize,
        dotGap
      )

      readonly property var ticks: Model.tempPlotTicks(width, windowMs, sampleMs)

      Repeater {
        model: plotArea.ticks.length

        Rectangle {
          required property int index
          readonly property var tick: plotArea.ticks[index]
          width: tick.major ? 1 : 1
          height: tick.major ? parent.height * 0.55 : parent.height * 0.35
          anchors.bottom: parent.bottom
          x: tick.x - (width / 2)
          color: tick.major ? Qt.rgba(root.dim.r, root.dim.g, root.dim.b, 0.45) : root.track
        }
      }

      Repeater {
        model: plotArea.dots.length

        Rectangle {
          required property int index
          readonly property var dot: plotArea.dots[index]
          width: dotSize
          height: dotSize
          radius: 1
          x: dot.x
          y: dot.y
          color: levelColor(dot.level)
          opacity: dot.latest ? 1 : (dot.carried ? 0.55 : 0.82)
        }
      }

      Rectangle {
        anchors.bottom: parent.bottom
        width: parent.width
        height: 1
        color: root.track
      }
    }

    Item {
      id: tickRow
      anchors.top: plotArea.bottom
      anchors.topMargin: Style.space(1)
      anchors.left: parent.left
      anchors.leftMargin: plotLeft
      anchors.right: parent.right
      anchors.rightMargin: plotRight
      height: Style.space(12)

      readonly property var ticks: Model.tempPlotTicks(width, windowMs, sampleMs)

      Repeater {
        model: tickRow.ticks.length

        Text {
          required property int index
          readonly property var tick: tickRow.ticks[index]
          visible: tick.major && tick.label !== ""
          text: tick.label === "0" ? "now" : ("-" + tick.label + "s")
          textFormat: Text.PlainText
          x: tick.x - (implicitWidth / 2)
          anchors.bottom: parent.bottom
          color: Qt.rgba(root.dim.r, root.dim.g, root.dim.b, 0.7)
          font.family: root.fontFamily
          font.pixelSize: Style.font.caption - 1
        }
      }
    }
  }
}
