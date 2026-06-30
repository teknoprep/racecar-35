package com.racedash.mobile.ui

import android.Manifest
import android.content.pm.PackageManager
import android.os.Bundle
import android.view.WindowManager
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.material3.Surface
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.core.content.ContextCompat
import androidx.lifecycle.viewmodel.compose.viewModel
import com.racedash.mobile.ui.theme.Bg
import com.racedash.mobile.ui.theme.RaceDashTheme
import com.racedash.mobile.vm.DashViewModel

enum class Page { DASH, SETTINGS, TRACK }

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        setContent {
            RaceDashTheme {
                Surface(modifier = Modifier.fillMaxSize(), color = Bg) {
                    AppRoot()
                }
            }
        }
    }
}

@Composable
private fun AppRoot() {
    val vm: DashViewModel = viewModel()

    // Request the runtime permissions we need, then tell the ViewModel what we got.
    val launcher = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { granted ->
        vm.onPermissions(
            location = granted[Manifest.permission.ACCESS_FINE_LOCATION] == true,
            audio = granted[Manifest.permission.RECORD_AUDIO] == true,
        )
    }
    val ctx = vm.getApplication<android.app.Application>()
    LaunchedEffect(Unit) {
        val hasLoc = ContextCompat.checkSelfPermission(
            ctx, Manifest.permission.ACCESS_FINE_LOCATION
        ) == PackageManager.PERMISSION_GRANTED
        val hasAud = ContextCompat.checkSelfPermission(
            ctx, Manifest.permission.RECORD_AUDIO
        ) == PackageManager.PERMISSION_GRANTED
        if (hasLoc && hasAud) {
            vm.onPermissions(hasLoc, hasAud)
        } else {
            launcher.launch(
                arrayOf(
                    Manifest.permission.ACCESS_FINE_LOCATION,
                    Manifest.permission.RECORD_AUDIO,
                )
            )
        }
    }

    // Silent OTA check on launch; only surfaces a dialog if an update exists.
    LaunchedEffect(Unit) { vm.checkForUpdate(manual = false) }

    var page by remember { mutableStateOf(Page.DASH) }
    when (page) {
        Page.DASH -> DashScreen(
            vm = vm,
            onOpenSettings = { page = Page.SETTINGS },
            onOpenTracks = { page = Page.TRACK },
        )
        Page.SETTINGS -> SettingsScreen(
            vm = vm,
            onBack = { page = Page.DASH },
        )
        Page.TRACK -> TrackPickerScreen(
            vm = vm,
            onDone = { page = Page.DASH },
        )
    }

    // OTA update prompts overlay every page.
    UpdateDialogHost(vm)
}
