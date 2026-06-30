package com.racedash.mobile.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.racedash.mobile.ui.theme.Accent
import com.racedash.mobile.ui.theme.Neutral
import com.racedash.mobile.ui.theme.Surface
import com.racedash.mobile.ui.theme.TextDim
import com.racedash.mobile.vm.DashViewModel

@Composable
fun SettingsScreen(vm: DashViewModel, onBack: () -> Unit) {
    val s by vm.settings.collectAsStateWithLifecycle()

    Column(modifier = Modifier.fillMaxSize().padding(12.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            IconButton(onClick = onBack) {
                Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "Back", tint = Neutral)
            }
            Text("Settings", color = Neutral, fontSize = 26.sp, fontWeight = FontWeight.Black)
        }
        Spacer(Modifier.height(4.dp))

        LazyColumn(modifier = Modifier.fillMaxSize()) {
            item { Header("Engine (drives acoustic RPM)") }
            item {
                Stepper("Cylinders", s.cylinders.toString()) { up ->
                    vm.settingsRepo.update { it.copy(cylinders = it.cylinders + if (up) 1 else -1) }
                }
            }
            item {
                Toggle("Stroke", s.strokes == 4, onLabel = "4-stroke", offLabel = "2-stroke") {
                    vm.settingsRepo.update { it.copy(strokes = if (it.strokes == 4) 2 else 4) }
                }
            }
            item {
                Stepper("Idle RPM", s.idleRpm.toString(), suffix = "rpm") { up ->
                    vm.settingsRepo.update { it.copy(idleRpm = it.idleRpm + if (up) 100 else -100) }
                }
            }
            item {
                Stepper("Redline RPM", s.redlineRpm.toString(), suffix = "rpm") { up ->
                    vm.settingsRepo.update { it.copy(redlineRpm = it.redlineRpm + if (up) 250 else -250) }
                }
            }

            item { Header("Display") }
            item {
                Stepper("RPM bar min", s.rpmBarMin.toString(), suffix = "rpm") { up ->
                    vm.settingsRepo.update { it.copy(rpmBarMin = it.rpmBarMin + if (up) 250 else -250) }
                }
            }
            item {
                Stepper("RPM bar max", s.rpmBarMax.toString(), suffix = "rpm") { up ->
                    vm.settingsRepo.update { it.copy(rpmBarMax = it.rpmBarMax + if (up) 250 else -250) }
                }
            }
            item {
                Stepper("Shift light RPM", s.shiftRpm.toString(), suffix = "rpm") { up ->
                    vm.settingsRepo.update { it.copy(shiftRpm = it.shiftRpm + if (up) 100 else -100) }
                }
            }
            item {
                Toggle("Speed units", s.useMph, onLabel = "MPH", offLabel = "KM/H") {
                    vm.settingsRepo.update { it.copy(useMph = !it.useMph) }
                }
            }

            item { Header("Audio") }
            item {
                Toggle("Estimate RPM from engine sound", s.audioRpmEnabled) {
                    vm.settingsRepo.update { it.copy(audioRpmEnabled = !it.audioRpmEnabled) }
                    vm.onSettingsChanged()
                }
            }

            item { Header("Recording") }
            item {
                Toggle("Auto-select closest track on START", s.autoSelectTrack) {
                    vm.settingsRepo.update { it.copy(autoSelectTrack = !it.autoSelectTrack) }
                }
            }
            item { Spacer(Modifier.height(24.dp)) }
        }
    }
}

@Composable
private fun Header(text: String) {
    Text(
        text,
        color = Accent,
        fontSize = 16.sp,
        fontWeight = FontWeight.Bold,
        modifier = Modifier.padding(top = 18.dp, bottom = 6.dp),
    )
}

@Composable
private fun Stepper(label: String, value: String, suffix: String = "", onStep: (Boolean) -> Unit) {
    Row(
        modifier = Modifier.fillMaxWidth().height(54.dp).clip(RoundedCornerShape(8.dp))
            .background(Surface).padding(horizontal = 14.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(label, color = Neutral, fontSize = 18.sp, modifier = Modifier.weight(1f))
        StepBtn("\u2212") { onStep(false) }
        Box(modifier = Modifier.width(120.dp), contentAlignment = Alignment.Center) {
            Text(
                if (suffix.isEmpty()) value else "$value $suffix",
                color = Neutral, fontSize = 20.sp, fontWeight = FontWeight.Bold,
                fontFamily = FontFamily.Monospace,
            )
        }
        StepBtn("+") { onStep(true) }
    }
    Spacer(Modifier.height(6.dp))
}

@Composable
private fun StepBtn(text: String, onClick: () -> Unit) {
    Box(
        modifier = Modifier.size(40.dp).clip(CircleShape).background(Accent),
        contentAlignment = Alignment.Center,
    ) {
        IconButton(onClick = onClick, modifier = Modifier.size(40.dp)) {
            Text(text, color = Color.Black, fontSize = 24.sp, fontWeight = FontWeight.Black)
        }
    }
}

@Composable
private fun Toggle(
    label: String,
    value: Boolean,
    onLabel: String = "On",
    offLabel: String = "Off",
    onToggle: () -> Unit,
) {
    Row(
        modifier = Modifier.fillMaxWidth().height(54.dp).clip(RoundedCornerShape(8.dp))
            .background(Surface).padding(horizontal = 14.dp),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.SpaceBetween,
    ) {
        Text(label, color = Neutral, fontSize = 18.sp, modifier = Modifier.weight(1f))
        Text(
            if (value) onLabel else offLabel,
            color = TextDim, fontSize = 15.sp, modifier = Modifier.padding(end = 10.dp),
        )
        Switch(checked = value, onCheckedChange = { onToggle() })
    }
    Spacer(Modifier.height(6.dp))
}
