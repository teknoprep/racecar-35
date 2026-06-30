package com.racedash.mobile.ui

import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxHeight
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.LocationOn
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.geometry.CornerRadius
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.racedash.mobile.ui.theme.Accent
import com.racedash.mobile.ui.theme.Better
import com.racedash.mobile.ui.theme.Danger
import com.racedash.mobile.ui.theme.Neutral
import com.racedash.mobile.ui.theme.Surface
import com.racedash.mobile.ui.theme.TextDim
import com.racedash.mobile.ui.theme.Warn
import com.racedash.mobile.ui.theme.Worse
import com.racedash.mobile.vm.DashViewModel
import com.racedash.mobile.vm.Fmt
import com.racedash.mobile.vm.GpsStatus
import kotlin.math.roundToInt

@Composable
fun DashScreen(
    vm: DashViewModel,
    onOpenSettings: () -> Unit,
    onOpenTracks: () -> Unit,
) {
    val s by vm.state.collectAsStateWithLifecycle()
    val cfg by vm.settings.collectAsStateWithLifecycle()

    Column(modifier = Modifier.fillMaxSize().padding(10.dp)) {
        RpmBar(
            rpm = s.rpm,
            conf = s.rpmConfidence,
            min = cfg.rpmBarMin,
            max = cfg.rpmBarMax,
            shift = cfg.shiftRpm,
            modifier = Modifier.fillMaxWidth().height(108.dp),
        )

        Spacer(Modifier.height(8.dp))

        Row(modifier = Modifier.fillMaxWidth().weight(1f)) {
            LapColumn(s, modifier = Modifier.weight(1f).fillMaxHeight())
            SpeedBlock(
                speed = s.speedMph,
                useMph = cfg.useMph,
                modifier = Modifier.weight(1.4f).fillMaxHeight(),
            )
            GpsColumn(s, modifier = Modifier.weight(1f).fillMaxHeight())
        }

        Spacer(Modifier.height(8.dp))

        BottomBar(
            recording = s.recording,
            sampleCount = s.sampleCount,
            trackName = s.trackName.ifBlank { s.nearestTrackName ?: "no track" },
            onStartStop = { vm.toggleRecording() },
            onOpenSettings = onOpenSettings,
            onOpenTracks = onOpenTracks,
        )
    }
}

@Composable
private fun RpmBar(
    rpm: Int, conf: Float, min: Int, max: Int, shift: Int, modifier: Modifier,
) {
    val frac = if (rpm < 0) 0f
    else ((rpm - min).toFloat() / (max - min).toFloat()).coerceIn(0f, 1f)
    val shiftFrac = ((shift - min).toFloat() / (max - min).toFloat()).coerceIn(0f, 1f)
    val overShift = rpm in (shift + 1)..Int.MAX_VALUE
    val barColor = when {
        rpm < 0 -> TextDim
        overShift -> Danger
        rpm >= shift - (shift - min) / 12 -> Warn
        else -> Accent
    }
    Box(modifier = modifier.clip(RoundedCornerShape(10.dp)).background(Surface)) {
        Canvas(modifier = Modifier.fillMaxSize()) {
            val h = size.height
            val w = size.width
            // Fill.
            if (frac > 0f) {
                drawRoundRect(
                    color = barColor,
                    size = androidx.compose.ui.geometry.Size(w * frac, h),
                    cornerRadius = CornerRadius(20f, 20f),
                )
            }
            // Shift marker.
            val x = w * shiftFrac
            drawLine(
                color = Color.White,
                start = androidx.compose.ui.geometry.Offset(x, 0f),
                end = androidx.compose.ui.geometry.Offset(x, h),
                strokeWidth = 4f,
            )
        }
        Row(
            modifier = Modifier.fillMaxSize().padding(horizontal = 18.dp),
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Text(
                text = if (rpm < 0) "----" else rpm.toString(),
                color = Neutral,
                fontSize = 56.sp,
                fontWeight = FontWeight.Black,
                fontFamily = FontFamily.Monospace,
            )
            Spacer(Modifier.width(8.dp))
            Column {
                Text("RPM", color = Neutral, fontSize = 18.sp, fontWeight = FontWeight.Bold)
                Text(
                    text = if (rpm < 0) "listening\u2026" else "mic ${(conf * 100).roundToInt()}%",
                    color = TextDim, fontSize = 12.sp,
                )
            }
            Spacer(Modifier.weight(1f))
            Text("$max", color = TextDim, fontSize = 14.sp)
        }
    }
}

@Composable
private fun SpeedBlock(speed: Float, useMph: Boolean, modifier: Modifier) {
    val value = if (useMph) speed else speed * 1.609344f
    val shown = value.roundToInt()
    Box(modifier = modifier, contentAlignment = Alignment.Center) {
        Column(horizontalAlignment = Alignment.CenterHorizontally) {
            Text(
                text = shown.toString(),
                color = Neutral,
                fontSize = 150.sp,
                fontWeight = FontWeight.Black,
                fontFamily = FontFamily.Monospace,
            )
            Text(
                text = if (useMph) "MPH" else "KM/H",
                color = TextDim, fontSize = 22.sp, fontWeight = FontWeight.Bold,
            )
        }
    }
}

@Composable
private fun LapColumn(s: com.racedash.mobile.vm.DashState, modifier: Modifier) {
    val lap = s.lap
    Column(modifier = modifier, verticalArrangement = Arrangement.Center) {
        Field("LAP", "#${lap.lapCount}")
        Field("CUR", Fmt.lapTime(lap.currentLapMs))
        Field("LAST", Fmt.lapTime(lap.lastLapMs))
        Field("BEST", Fmt.lapTime(lap.bestLapMs), Accent)
        val deltaColor = deltaColor(lap.deltaMs, lap.deltaValid)
        Field("PRED", Fmt.lapTime(if (lap.deltaValid) lap.predictedMs else -1), deltaColor)
        Field("DELTA", if (lap.deltaValid) Fmt.delta(lap.deltaMs) else "--", deltaColor)
    }
}

@Composable
private fun GpsColumn(s: com.racedash.mobile.vm.DashState, modifier: Modifier) {
    val statusColor = when (s.gpsStatus) {
        GpsStatus.OK -> Better
        GpsStatus.STALE -> Warn
        GpsStatus.ACQUIRING -> Warn
        GpsStatus.OFF -> Danger
    }
    Column(modifier = modifier, verticalArrangement = Arrangement.Center, horizontalAlignment = Alignment.End) {
        Field("GPS", s.gpsStatus.name, statusColor, end = true)
        Field("SATS", s.sats.toString(), end = true)
        Field("HDG", "${s.headingDeg.roundToInt()}\u00B0", end = true)
        Field("LAT", String.format("%.5f", s.lat), end = true)
        Field("LON", String.format("%.5f", s.lon), end = true)
        Field("LAT G", String.format("%+.2f", s.latG), end = true)
    }
}

@Composable
private fun Field(label: String, value: String, valueColor: Color = Neutral, end: Boolean = false) {
    Column(
        modifier = Modifier.fillMaxWidth().padding(vertical = 4.dp),
        horizontalAlignment = if (end) Alignment.End else Alignment.Start,
    ) {
        Text(label, color = TextDim, fontSize = 13.sp, fontWeight = FontWeight.Bold)
        Text(
            value, color = valueColor, fontSize = 26.sp,
            fontWeight = FontWeight.Bold, fontFamily = FontFamily.Monospace,
        )
    }
}

@Composable
private fun BottomBar(
    recording: Boolean,
    sampleCount: Int,
    trackName: String,
    onStartStop: () -> Unit,
    onOpenSettings: () -> Unit,
    onOpenTracks: () -> Unit,
) {
    Row(
        modifier = Modifier.fillMaxWidth().height(64.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Button(
            onClick = onStartStop,
            modifier = Modifier.fillMaxHeight().width(160.dp),
            shape = RoundedCornerShape(10.dp),
            colors = ButtonDefaults.buttonColors(
                containerColor = if (recording) Worse else Accent,
                contentColor = Color.Black,
            ),
        ) {
            Text(
                if (recording) "STOP" else "START",
                fontSize = 24.sp, fontWeight = FontWeight.Black,
            )
        }
        Spacer(Modifier.width(14.dp))
        Column {
            Text(trackName, color = Neutral, fontSize = 18.sp, fontWeight = FontWeight.Bold)
            Text(
                if (recording) "REC \u25CF  $sampleCount samples" else "idle",
                color = if (recording) Worse else TextDim, fontSize = 13.sp,
            )
        }
        Spacer(Modifier.weight(1f))
        IconButton(onClick = onOpenTracks, modifier = Modifier.fillMaxHeight()) {
            Icon(Icons.Filled.LocationOn, contentDescription = "Tracks", tint = Neutral)
        }
        IconButton(onClick = onOpenSettings, modifier = Modifier.fillMaxHeight()) {
            Icon(Icons.Filled.Settings, contentDescription = "Settings", tint = Neutral)
        }
    }
}

private fun deltaColor(deltaMs: Long, valid: Boolean): Color {
    if (!valid) return Neutral
    return when {
        deltaMs < -50 -> Better
        deltaMs > 50 -> Worse
        else -> Neutral
    }
}
