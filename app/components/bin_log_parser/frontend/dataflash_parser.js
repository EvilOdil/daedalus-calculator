/**
 * Client-side ArduPilot dataflash (.bin/.log) parser.
 *
 * A from-scratch port of the *subset* of pymavlink's `DFReader_binary` this
 * app needs - independently written from the public, documented dataflash
 * wire format (sync bytes, FMT-message-driven per-type schemas, the format
 * -character table), not a translation of pymavlink's or UAVLogViewer's
 * source (both GPL-licensed; this file is not). It exists so a multi-hundred
 * -MB log never has to leave the browser: only the tiny `FlightLogSummary`
 * -shaped result this returns gets sent to the server. See
 * `dronecalc/ardupilot_log.py` for the server-side reference implementation
 * this is meant to match field-for-field.
 *
 * Every record is `[0xA3][0x95][msg_id: u8][body: (FMT.Length - 3) bytes]`.
 * The FMT message itself always has id 128 and a fixed layout - that fixes
 * the bootstrap problem of "how do I decode the message that tells me how to
 * decode messages". Every other id's layout is learned from the FMT records
 * the log itself carries, in file order, exactly as pymavlink does it - so
 * this generalises across firmware versions without hardcoding ids.
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) {
    module.exports = factory();
  } else {
    root.DaedalusLogParser = factory();
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  const HEAD1 = 0xa3;
  const HEAD2 = 0x95;
  const FMT_MSG_ID = 128;
  const FMT_FIELD_NAMES = ["Type", "Length", "Name", "Format", "Columns"];

  //: (struct byte size, decode(dataview, offset) -> raw value) per FMT format
  //: character. Verified against the real Format strings a modern ArduPilot
  //: log carries (dumped from an actual flight log via pymavlink), not
  //: guessed. `g`/`a` are declared but not decoded (never used by any
  //: message type this parser reads) - encountering one throws a clear
  //: error rather than silently misreading bytes.
  const FIELD_CODEC = {
    a: { size: 64, read: readBytesUnsupported },
    b: { size: 1, read: (dv, o) => dv.getInt8(o) },
    B: { size: 1, read: (dv, o) => dv.getUint8(o) },
    g: { size: 2, read: readBytesUnsupported },
    h: { size: 2, read: (dv, o) => dv.getInt16(o, true) },
    H: { size: 2, read: (dv, o) => dv.getUint16(o, true) },
    i: { size: 4, read: (dv, o) => dv.getInt32(o, true) },
    I: { size: 4, read: (dv, o) => dv.getUint32(o, true) },
    f: { size: 4, read: (dv, o) => dv.getFloat32(o, true) },
    n: { size: 4, read: (dv, o) => readCString(dv, o, 4) },
    N: { size: 16, read: (dv, o) => readCString(dv, o, 16) },
    Z: { size: 64, read: (dv, o) => readCString(dv, o, 64) },
    c: { size: 2, read: (dv, o) => dv.getInt16(o, true) * 0.01 },
    C: { size: 2, read: (dv, o) => dv.getUint16(o, true) * 0.01 },
    e: { size: 4, read: (dv, o) => dv.getInt32(o, true) * 0.01 },
    E: { size: 4, read: (dv, o) => dv.getUint32(o, true) * 0.01 },
    L: { size: 4, read: (dv, o) => dv.getInt32(o, true) * 1e-7 },
    d: { size: 8, read: (dv, o) => dv.getFloat64(o, true) },
    M: { size: 1, read: (dv, o) => dv.getInt8(o) },
    // 64-bit fields as plain JS numbers via two u32 reads - safe up to
    // 2^53us (~285 years of boot time), and avoids BigInt arithmetic
    // everywhere else this value is used (timestamps, mostly).
    q: { size: 8, read: (dv, o) => readU64AsNumber(dv, o) },
    Q: { size: 8, read: (dv, o) => readU64AsNumber(dv, o) },
  };

  function readU64AsNumber(dv, o) {
    const lo = dv.getUint32(o, true);
    const hi = dv.getUint32(o + 4, true);
    return hi * 4294967296 + lo;
  }

  function readCString(dv, offset, maxLen) {
    let end = offset;
    const limit = offset + maxLen;
    while (end < limit && dv.getUint8(end) !== 0) end++;
    let s = "";
    for (let i = offset; i < end; i++) s += String.fromCharCode(dv.getUint8(i));
    return s;
  }

  function readBytesUnsupported() {
    throw new Error("unsupported dataflash field format code (array/half-float)");
  }

  //: Same keyword set `_classify_msg_level` uses server-side.
  const WARN_KEYWORDS = ["fail", "error", "crash", "abort", "glitch"];
  const ERR_SUBSYS_THRUST_LOSS = 25;
  const ERR_SUBSYS_CRASH_CHECK = 12;
  const ERR_SUBSYS_GEOFENCE = 9;
  const FENCE_TYPE_BITS = [
    [1, "max altitude"],
    [2, "circle"],
    [4, "polygon"],
    [8, "min altitude"],
  ];
  const CMD_TAKEOFF = 22;
  const CMD_LAND = 21;

  const MAX_EVENTS = 500;
  const MAX_MISSION_EVENTS = 150;
  const MAX_LANDING_TARGET_POINTS = 300;
  const MAX_SENSOR_SERIES_POINTS = 500;
  const MAX_SERIES_POINTS = 500;
  const NAV_SPEED_THRESHOLD_MPS = 3.0;
  const NAV_MIN_SUSTAIN_SAMPLES = 3;
  const EARTH_RADIUS_M = 6371000.0;

  //: Message types this parser (and the server-side one) actually reads.
  //: Everything else is skipped by record length without being decoded.
  const WANTED_TYPES = new Set([
    "BAT", "CURR", "MSG", "ERR", "STAT", "GPS", "CMD", "PL", "RFND", "OF", "CTUN",
  ]);

  //: GPS week epoch (1980-01-06 00:00:00 UTC) plus the current GPS-UTC leap
  //: -second offset (18s) - both public constants, not the reference
  //: implementation's specific expression of them.
  const GPS_EPOCH_UNIX_S = Date.UTC(1980, 0, 6) / 1000;
  const GPS_UTC_LEAP_S = 18;

  function gpsTimeToUnixSeconds(week, tow_ms) {
    return GPS_EPOCH_UNIX_S + week * 604800 + tow_ms * 0.001 - GPS_UTC_LEAP_S;
  }

  class LogParseError extends Error {}

  /**
   * Parse one FMT record's body into a {name, length, fields:[{name, code,
   * size, offset}]} descriptor. `length` is the FULL record length
   * (3-byte header included), exactly as the log declares it - used to skip
   * every record of this type without decoding it, when it isn't wanted.
   */
  function parseFmtRecord(dv, bodyOffset) {
    const type = dv.getUint8(bodyOffset);
    const length = dv.getUint8(bodyOffset + 1);
    const name = readCString(dv, bodyOffset + 2, 4);
    const formatStr = readCString(dv, bodyOffset + 6, 16);
    const columnsStr = readCString(dv, bodyOffset + 22, 64);
    const columnNames = columnsStr ? columnsStr.split(",") : [];

    const fields = [];
    let fieldOffset = 0;
    for (let i = 0; i < formatStr.length; i++) {
      const code = formatStr[i];
      const codec = FIELD_CODEC[code];
      if (!codec) continue; // unknown code - field becomes unreadable, skipped below
      fields.push({
        name: columnNames[i] !== undefined ? columnNames[i] : `f${i}`,
        code,
        size: codec.size,
        offset: fieldOffset,
      });
      fieldOffset += codec.size;
    }
    return { id: type, name, length, fields, bodySize: fieldOffset };
  }

  /** Decode one record's body into `{fieldName: value, ...}` per its FMT. */
  function decodeRecord(dv, bodyOffset, fmt) {
    const out = {};
    for (const f of fmt.fields) {
      const codec = FIELD_CODEC[f.code];
      out[f.name] = codec.read(dv, bodyOffset + f.offset);
    }
    return out;
  }

  function classifyMsgLevel(text) {
    const lowered = text.toLowerCase();
    return WARN_KEYWORDS.some((k) => lowered.includes(k)) ? "warning" : "info";
  }

  function decodeFenceBitmask(ecode) {
    if (!ecode) return "cleared";
    const names = FENCE_TYPE_BITS.filter(([bit]) => ecode & bit).map(([, name]) => name);
    return names.length ? names.join(" + ") : `code ${ecode}`;
  }

  function errEvent(t_s, subsys, ecode) {
    if (subsys === ERR_SUBSYS_THRUST_LOSS) {
      return {
        t_s, level: "error", subsystem: "Thrust loss check",
        message: "Potential thrust loss — sustained high throttle with excess "
          + "attitude error (a failed motor/propeller/ESC signature)",
      };
    }
    if (subsys === ERR_SUBSYS_CRASH_CHECK) {
      return { t_s, level: "error", subsystem: "Crash check", message: "Crash detected — motors disarmed" };
    }
    if (subsys === ERR_SUBSYS_GEOFENCE) {
      const detail = decodeFenceBitmask(ecode);
      return {
        t_s, level: ecode ? "error" : "info", subsystem: "Geofence",
        message: ecode ? `Breach (${detail})` : "Breach cleared",
      };
    }
    return {
      t_s, level: "error",
      subsystem: subsys !== null && subsys !== undefined ? `Subsys ${subsys}` : null,
      message: ecode !== null && ecode !== undefined ? `Error code ${ecode}` : "error logged",
    };
  }

  function missionCmdLabel(cid, cnum) {
    if (cid === CMD_TAKEOFF) return ["T", "Takeoff"];
    if (cid === CMD_LAND) return ["L", "Land"];
    const label = cnum !== null && cnum !== undefined ? String(cnum) : "?";
    const command = cnum !== null && cnum !== undefined ? `Waypoint ${cnum}` : "Command";
    return [label, command];
  }

  function haversineM(lat1, lon1, lat2, lon2) {
    const toRad = (d) => (d * Math.PI) / 180;
    const phi1 = toRad(lat1);
    const phi2 = toRad(lat2);
    const dphi = toRad(lat2 - lat1);
    const dlambda = toRad(lon2 - lon1);
    const a = Math.sin(dphi / 2) ** 2 + Math.cos(phi1) * Math.cos(phi2) * Math.sin(dlambda / 2) ** 2;
    return 2 * EARTH_RADIUS_M * Math.asin(Math.min(1, Math.sqrt(a)));
  }

  /** Uniform-stride decimation, always keeping the final sample - mirrors
   * `_downsample`/`_downsample_series` exactly, boundary values included. */
  function downsampleRows(rows, target, build) {
    if (!rows.length) return [];
    const stride = Math.max(1, Math.floor(rows.length / target));
    const picked = [];
    for (let i = 0; i < rows.length; i += stride) picked.push(rows[i]);
    if (picked[picked.length - 1] !== rows[rows.length - 1]) picked.push(rows[rows.length - 1]);
    return picked.map(build);
  }

  function computeRateSeries(timestamps, maxPoints) {
    if (timestamps.length < 2) return [];
    const points = [];
    for (let i = 1; i < timestamps.length; i++) {
      const t0 = timestamps[i - 1];
      const t1 = timestamps[i];
      if (t1 > t0) points.push({ t_s: t1, hz: 1.0 / (t1 - t0) });
    }
    return downsampleRows(points, maxPoints, (p) => p);
  }

  /** First-to-last run of >= minSamples consecutive fixes at/above
   * threshold - "cruise" as a cheap, explainable stand-in for real
   * takeoff/landing phase detection. Mirrors `_sustained_speed_window`. */
  function sustainedSpeedWindow(speeds, threshold, minSamples) {
    const runs = [];
    let runStart = null;
    for (let i = 0; i < speeds.length; i++) {
      const fast = speeds[i] !== null && speeds[i] !== undefined && speeds[i] >= threshold;
      if (fast && runStart === null) runStart = i;
      else if (!fast && runStart !== null) {
        if (i - runStart >= minSamples) runs.push([runStart, i - 1]);
        runStart = null;
      }
    }
    if (runStart !== null && speeds.length - runStart >= minSamples) runs.push([runStart, speeds.length - 1]);
    if (!runs.length) return null;
    return [runs[0][0], runs[runs.length - 1][1]];
  }

  function computeDistances(track, warnings) {
    if (track.length < 2) {
      warnings.push("fewer than 2 GPS fixes with a 3D lock — cannot compute distance travelled");
      return { total: null, nav: null, navDuration: null };
    }
    let total = 0;
    for (let i = 1; i < track.length; i++) {
      total += haversineM(track[i - 1][1], track[i - 1][2], track[i][1], track[i][2]);
    }
    const speeds = track.map((r) => r[3]);
    const window = sustainedSpeedWindow(speeds, NAV_SPEED_THRESHOLD_MPS, NAV_MIN_SUSTAIN_SAMPLES);
    if (window === null) {
      warnings.push(
        `ground speed never sustained ${NAV_SPEED_THRESHOLD_MPS} m/s for ${NAV_MIN_SUSTAIN_SAMPLES} `
        + "consecutive GPS fixes — no navigation-phase window found (e.g. a hover-only test)"
      );
      return { total, nav: null, navDuration: null };
    }
    const [start, end] = window;
    let nav = 0;
    for (let i = start; i < end; i++) {
      nav += haversineM(track[i][1], track[i][2], track[i + 1][1], track[i + 1][2]);
    }
    return { total, nav, navDuration: track[end][0] - track[start][0] };
  }

  function nearestReading(rows, t_s, useVolt) {
    if (t_s === null || t_s === undefined) return null;
    let best = null;
    let bestDelta = Infinity;
    for (const [t, v, i] of rows) {
      const val = useVolt ? v : i;
      if (val === null || val === undefined) continue;
      const delta = Math.abs(t - t_s);
      if (delta < bestDelta) {
        bestDelta = delta;
        best = val;
      }
    }
    return best;
  }

  function summariseBattery(rows, sagCandidates, mahFirst, mahLast, armedTS, disarmedTS, warnings) {
    if (!rows.length) {
      return {
        v_start: null, v_end: null, v_min: null, v_min_t_s: null, sag_v: null,
        i_max: null, mah_consumed: null, energy_wh: null, energy_wh_is_estimated: false,
      };
    }
    const volts = rows.filter((r) => r[1] !== null && r[1] !== undefined).map((r) => [r[0], r[1]]);
    const currs = rows.filter((r) => r[2] !== null && r[2] !== undefined).map((r) => r[2]);

    const v_start = nearestReading(rows, armedTS, true);
    const v_end = nearestReading(rows, disarmedTS, true);

    let v_min = null;
    let v_min_t_s = null;
    if (volts.length) {
      const minRow = volts.reduce((a, b) => (b[1] < a[1] ? b : a));
      v_min_t_s = minRow[0];
      v_min = minRow[1];
    }

    let sag_v;
    if (sagCandidates.length) {
      sag_v = Math.max(...sagCandidates);
    } else if (v_start !== null && v_min !== null) {
      sag_v = v_start - v_min;
      warnings.push("battery has no VoltR (resting-voltage) samples — sag is v_start - v_min instead");
    } else {
      sag_v = null;
    }

    const i_max = currs.length ? Math.max(...currs) : null;

    let mah_consumed = null;
    if (mahFirst !== null && mahLast !== null) {
      mah_consumed = mahLast >= mahFirst ? mahLast - mahFirst : mahLast;
    }

    let energy_wh = null;
    let is_estimated = false;
    const paired = rows.filter((r) => r[1] !== null && r[1] !== undefined && r[2] !== null && r[2] !== undefined);
    if (paired.length >= 2) {
      energy_wh = 0;
      for (let k = 1; k < paired.length; k++) {
        const dt = paired[k][0] - paired[k - 1][0];
        if (dt <= 0) continue;
        const p0 = paired[k - 1][1] * paired[k - 1][2];
        const p1 = paired[k][1] * paired[k][2];
        energy_wh += ((p0 + p1) / 2) * dt / 3600.0;
      }
    } else if (mah_consumed !== null && volts.length) {
      const meanV = volts.reduce((s, v) => s + v[1], 0) / volts.length;
      energy_wh = (mah_consumed / 1000.0) * meanV;
      is_estimated = true;
    }

    return {
      v_start, v_end, v_min, v_min_t_s, sag_v, i_max, mah_consumed,
      energy_wh, energy_wh_is_estimated: is_estimated,
    };
  }

  function deriveDates(firstEpochAbs, armedTS) {
    if (firstEpochAbs === null) return [null, null];
    const recordedS = firstEpochAbs + armedTS;
    const d = new Date(recordedS * 1000);
    if (Number.isNaN(d.getTime()) || d.getUTCFullYear() < 2000) return [null, null];
    const pad = (n) => String(n).padStart(2, "0");
    const log_date = `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}`;
    const flown_at = `${log_date} ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`;
    return [log_date, flown_at];
  }

  /**
   * Parse an ArduPilot dataflash log from an ArrayBuffer.
   *
   * `onProgress`, if given, is called with 0-100 periodically during the
   * single forward pass over the file - the equivalent of
   * `parse_log(..., progress_callback=...)` server-side.
   *
   * Returns a plain object with exactly the field names
   * `dronecalc.missions.FlightLogSummary` expects (minus `source_filename`/
   * `parsed_at`, which the caller fills in - this function doesn't know the
   * original filename or wall-clock parse time).
   */
  function parseDataflashLog(arrayBuffer, options) {
    options = options || {};
    const onProgress = options.onProgress || null;
    const maxSeriesPoints = options.maxSeriesPoints || MAX_SERIES_POINTS;

    const dv = new DataView(arrayBuffer);
    const len = arrayBuffer.byteLength;

    const formatsById = new Map();
    const warnings = [];

    const batteryRows = []; // [t_s, volt|null, curr|null]
    const sagCandidates = [];
    let mahFirst = null;
    let mahLast = null;

    const events = [];
    let truncatedEvents = false;
    let thrustLossEvents = 0;
    let crashEvents = 0;
    let geofenceBreachEvents = 0;

    const missionEvents = [];
    let truncatedMissionEvents = false;

    const landingTargetTimestamps = [];
    let landingTargetSamples = 0;
    let lastMeasMs = null;

    const rangefinderRows = [];
    const flowXRows = [];
    const flowYRows = [];
    const throttleRows = [];

    let firstTimeUS = null;
    let lastTimeUS = null;
    let armedTimeUS = null;
    let disarmedTimeUS = null;
    let lastArmedState = null;

    let timebaseS = null; // absolute-epoch anchor: epoch(TimeUS=0) in Unix seconds

    let takeoffLatLon = null;
    let landingLatLon = null;
    const gpsTrack = []; // [t_s, lat, lng, spd|null]

    const tsOf = (timeUS) => (timeUS - firstTimeUS) * 1e-6;

    let offset = 0;
    let lastProgressReportOffset = 0;
    while (offset + 3 <= len) {
      if (dv.getUint8(offset) !== HEAD1 || dv.getUint8(offset + 1) !== HEAD2) {
        offset += 1;
        continue;
      }
      const msgId = dv.getUint8(offset + 2);
      const fmt = msgId === FMT_MSG_ID
        ? { id: FMT_MSG_ID, name: "FMT", length: 89 }
        : formatsById.get(msgId);
      // A record can never be shorter than its own 3-byte header - a `Length`
      // of 0-2 only happens on a corrupt/truncated FMT definition. Treating
      // it as "unknown" and resyncing by one byte (same as `!fmt` below) is
      // required for correctness, not just tidiness: falling through would
      // advance `offset` by 0 and spin on this record forever.
      if (!fmt || !(fmt.length >= 3)) {
        offset += 1; // unknown id (FMT not seen yet, or a type we never register), or corrupt Length - resync
        continue;
      }
      if (offset + fmt.length > len) break; // truncated tail record

      const bodyOffset = offset + 3;

      if (fmt.name === "FMT") {
        const parsed = parseFmtRecord(dv, bodyOffset);
        formatsById.set(parsed.id, parsed);
      } else if (WANTED_TYPES.has(fmt.name)) {
        const rec = decodeRecord(dv, bodyOffset, fmt);
        const timeUS = rec.TimeUS;
        if (typeof timeUS === "number") {
          if (firstTimeUS === null) firstTimeUS = timeUS;
          lastTimeUS = timeUS;
        }
        const t_s = firstTimeUS === null ? 0 : tsOf(timeUS);

        if (fmt.name === "BAT" || fmt.name === "CURR") {
          const instance = rec.Inst !== undefined ? rec.Inst : (rec.Instance || 0);
          if (!instance) {
            const volt = rec.Volt !== undefined ? rec.Volt : null;
            const curr = rec.Curr !== undefined ? rec.Curr : null;
            const voltR = rec.VoltR !== undefined ? rec.VoltR : null;
            const currTot = rec.CurrTot !== undefined ? rec.CurrTot : null;
            if (volt !== null || curr !== null) batteryRows.push([t_s, volt, curr]);
            if (volt !== null && voltR !== null) sagCandidates.push(voltR - volt);
            if (currTot !== null) {
              if (mahFirst === null) mahFirst = currTot;
              mahLast = currTot;
            }
          }
        } else if (fmt.name === "MSG") {
          if (events.length < MAX_EVENTS) {
            const text = rec.Message || "";
            events.push({ t_s, level: classifyMsgLevel(text), subsystem: null, message: text });
          } else {
            truncatedEvents = true;
          }
        } else if (fmt.name === "ERR") {
          const subsys = rec.Subsys !== undefined ? rec.Subsys : null;
          const ecode = rec.ECode !== undefined ? rec.ECode : null;
          if (subsys === ERR_SUBSYS_THRUST_LOSS) thrustLossEvents++;
          else if (subsys === ERR_SUBSYS_CRASH_CHECK) crashEvents++;
          else if (subsys === ERR_SUBSYS_GEOFENCE && ecode) geofenceBreachEvents++;
          if (events.length < MAX_EVENTS) events.push(errEvent(t_s, subsys, ecode));
          else truncatedEvents = true;
        } else if (fmt.name === "STAT") {
          const armed = rec.Armed !== undefined ? rec.Armed : null;
          if (armed !== null) {
            if (lastArmedState === 0 && armed === 1 && armedTimeUS === null) armedTimeUS = timeUS;
            else if (lastArmedState === 1 && armed === 0) disarmedTimeUS = timeUS;
            lastArmedState = armed;
          }
        } else if (fmt.name === "GPS") {
          const status = rec.Status || 0;
          const lat = rec.Lat;
          const lng = rec.Lng;
          if (status >= 3 && lat && lng) {
            if (takeoffLatLon === null) takeoffLatLon = [lat, lng];
            landingLatLon = [lat, lng];
            gpsTrack.push([t_s, lat, lng, rec.Spd !== undefined ? rec.Spd : null]);
          }
          if (timebaseS === null && timeUS && rec.GWk) {
            timebaseS = gpsTimeToUnixSeconds(rec.GWk, rec.GMS) - timeUS * 1e-6;
          }
        } else if (fmt.name === "CMD") {
          if (missionEvents.length < MAX_MISSION_EVENTS) {
            const [label, command] = missionCmdLabel(rec.CId, rec.CNum);
            missionEvents.push({ t_s, label, command });
          } else {
            truncatedMissionEvents = true;
          }
        } else if (fmt.name === "PL") {
          const meas = rec.LastMeasMS !== undefined ? rec.LastMeasMS : null;
          if (meas !== null) {
            if (lastMeasMs !== null && meas !== lastMeasMs) {
              landingTargetTimestamps.push(t_s);
              landingTargetSamples++;
            }
            lastMeasMs = meas;
          }
        } else if (fmt.name === "RFND") {
          const instance = rec.Instance || 0;
          const dist = rec.Dist;
          if (instance === 0 && dist !== undefined) rangefinderRows.push([t_s, dist]);
        } else if (fmt.name === "OF") {
          if (rec.flowX !== undefined) flowXRows.push([t_s, rec.flowX]);
          if (rec.flowY !== undefined) flowYRows.push([t_s, rec.flowY]);
        } else if (fmt.name === "CTUN") {
          if (rec.ThO !== undefined) throttleRows.push([t_s, rec.ThO * 100.0]);
        }
      }

      offset += fmt.length;
      if (onProgress && offset - lastProgressReportOffset > 1 << 20) {
        lastProgressReportOffset = offset;
        onProgress(Math.min(100, Math.round((offset / len) * 100)));
      }
    }
    if (onProgress) onProgress(100);

    if (firstTimeUS === null) {
      throw new LogParseError(
        "did not contain any recognisable BAT/CURR/MSG/STAT/GPS messages"
      );
    }
    if (truncatedEvents) warnings.push(`more than ${MAX_EVENTS} MSG/ERR lines — only the first ${MAX_EVENTS} were kept`);
    if (truncatedMissionEvents) {
      warnings.push(`more than ${MAX_MISSION_EVENTS} mission commands — only the first ${MAX_MISSION_EVENTS} were kept`);
    }
    if (!batteryRows.length) warnings.push("no battery voltage/current data (BAT/CURR messages) found in this log");

    const durationS = tsOf(lastTimeUS);
    const armedTS = armedTimeUS === null ? 0.0 : tsOf(armedTimeUS);
    const disarmedTS = disarmedTimeUS === null ? durationS : tsOf(disarmedTimeUS);

    const battery = summariseBattery(batteryRows, sagCandidates, mahFirst, mahLast, armedTS, disarmedTS, warnings);

    const firstEpochAbs = timebaseS === null ? null : timebaseS + firstTimeUS * 1e-6;
    const [log_date, flown_at] = deriveDates(firstEpochAbs, armedTS);

    const { total: totalDistanceM, nav: navigationDistanceM, navDuration: navigationDurationS } =
      computeDistances(gpsTrack, warnings);

    const series = downsampleRows(batteryRows, maxSeriesPoints, ([t_s, v, i]) => ({
      t_s, voltage_v: v, current_a: i, power_w: v !== null && i !== null ? v * i : null,
    }));
    const landingTargetRate = computeRateSeries(landingTargetTimestamps, MAX_LANDING_TARGET_POINTS);
    const rangefinderDistanceM = downsampleRows(rangefinderRows, MAX_SENSOR_SERIES_POINTS, ([t_s, value]) => ({ t_s, value }));
    const opticalFlowRateX = downsampleRows(flowXRows, MAX_SENSOR_SERIES_POINTS, ([t_s, value]) => ({ t_s, value }));
    const opticalFlowRateY = downsampleRows(flowYRows, MAX_SENSOR_SERIES_POINTS, ([t_s, value]) => ({ t_s, value }));
    const throttlePct = downsampleRows(throttleRows, MAX_SENSOR_SERIES_POINTS, ([t_s, value]) => ({ t_s, value }));

    return {
      log_date, flown_at,
      duration_s: durationS, armed_t_s: armedTS, disarmed_t_s: disarmedTS,
      battery, series, events,
      takeoff_latlon: takeoffLatLon, landing_latlon: landingLatLon,
      total_distance_m: totalDistanceM, navigation_distance_m: navigationDistanceM,
      navigation_duration_s: navigationDurationS,
      thrust_loss_events: thrustLossEvents, crash_events: crashEvents,
      geofence_breach_events: geofenceBreachEvents, mission_events: missionEvents,
      landing_target_rate: landingTargetRate, landing_target_samples: landingTargetSamples,
      rangefinder_distance_m: rangefinderDistanceM,
      optical_flow_rate_x: opticalFlowRateX, optical_flow_rate_y: opticalFlowRateY,
      throttle_pct: throttlePct,
      warnings,
    };
  }

  return { parseDataflashLog, LogParseError };
});
