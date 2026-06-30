package com.racedash.mobile.ui

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import com.racedash.mobile.data.Track
import com.racedash.mobile.data.Tracks
import com.racedash.mobile.ui.theme.Accent
import com.racedash.mobile.ui.theme.Better
import com.racedash.mobile.ui.theme.Neutral
import com.racedash.mobile.ui.theme.Surface
import com.racedash.mobile.ui.theme.TextDim
import com.racedash.mobile.vm.DashViewModel

@Composable
fun TrackPickerScreen(vm: DashViewModel, onDone: () -> Unit) {
    val nearest = remember { vm.nearest() }
    var selected by remember { mutableStateOf(nearest?.first ?: Tracks.REAL.first()) }

    // Nearest track first (if we have a fix), then the rest alphabetically, with
    // the synthetic "(no track / unknown)" row always pinned last.
    val ordered = remember(nearest) {
        val rest = Tracks.ALL
            .filter { it != nearest?.first }
            .sortedWith(compareBy({ it.isUnknown }, { it.name.lowercase() }))
        if (nearest != null) listOf(nearest.first) + rest else rest
    }

    Column(modifier = Modifier.fillMaxSize().padding(12.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            IconButton(onClick = onDone) {
                Icon(Icons.AutoMirrored.Filled.ArrowBack, contentDescription = "Back", tint = Neutral)
            }
            Text("Pick track", color = Neutral, fontSize = 26.sp, fontWeight = FontWeight.Black)
            Spacer(Modifier.weight(1f))
            Button(
                onClick = { vm.setStartFinishHere() },
                colors = ButtonDefaults.buttonColors(containerColor = Surface, contentColor = Neutral),
                shape = RoundedCornerShape(8.dp),
            ) { Text("Set S/F here") }
        }
        Spacer(Modifier.height(6.dp))

        LazyColumn(modifier = Modifier.fillMaxSize().weight(1f)) {
            items(ordered) { t ->
                val isNearest = nearest != null && t == nearest.first
                val dist = if (isNearest) nearest?.second else null
                TrackRow(
                    track = t,
                    selected = t == selected,
                    isNearest = isNearest,
                    distKm = dist,
                ) { selected = t }
            }
            item { Spacer(Modifier.height(12.dp)) }
        }

        Button(
            onClick = { vm.selectTrack(selected); onDone() },
            modifier = Modifier.fillMaxWidth().height(60.dp),
            shape = RoundedCornerShape(10.dp),
            colors = ButtonDefaults.buttonColors(containerColor = Accent, contentColor = Color.Black),
        ) {
            Text("CONFIRM  \u2014  ${selected.name}", fontSize = 20.sp, fontWeight = FontWeight.Black)
        }
    }
}

@Composable
private fun TrackRow(
    track: Track,
    selected: Boolean,
    isNearest: Boolean,
    distKm: Double?,
    onClick: () -> Unit,
) {
    Row(
        modifier = Modifier.fillMaxWidth().height(56.dp).padding(vertical = 4.dp)
            .clip(RoundedCornerShape(8.dp))
            .background(if (selected) Surface else Color(0xFF11161D))
            .clickable { onClick() }
            .padding(horizontal = 16.dp),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.SpaceBetween,
    ) {
        Text(
            track.name,
            color = if (isNearest) Better else Neutral,
            fontSize = 20.sp,
            fontWeight = if (selected) FontWeight.Black else FontWeight.Normal,
        )
        if (distKm != null) {
            Text(
                "closest \u00B7 ${String.format("%.1f", distKm)} km",
                color = Better, fontSize = 14.sp,
            )
        } else if (selected) {
            Text("selected", color = TextDim, fontSize = 14.sp)
        }
    }
}
