package com.racedash.mobile.ui.theme

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.ui.graphics.Color

val Bg = Color(0xFF0E1116)
val Surface = Color(0xFF161B22)
val Accent = Color(0xFF00E5A0)
val AccentDim = Color(0xFF0B8F66)
val Warn = Color(0xFFFFB020)
val Danger = Color(0xFFFF4D4D)
val Better = Color(0xFF38D66B)
val Worse = Color(0xFFFF4D4D)
val Neutral = Color(0xFFE6EDF3)
val TextDim = Color(0xFF8B98A5)

private val DarkColors = darkColorScheme(
    primary = Accent,
    onPrimary = Color(0xFF06231A),
    secondary = AccentDim,
    background = Bg,
    onBackground = Neutral,
    surface = Surface,
    onSurface = Neutral,
    surfaceVariant = Color(0xFF1F2630),
    onSurfaceVariant = TextDim,
    error = Danger,
)

@Composable
fun RaceDashTheme(content: @Composable () -> Unit) {
    // Always dark — this is a track dash.
    MaterialTheme(colorScheme = DarkColors, content = content)
}
