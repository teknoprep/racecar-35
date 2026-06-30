package com.racedash.mobile.ui

import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.racedash.mobile.vm.DashViewModel
import com.racedash.mobile.vm.UpdatePhase

/** Overlay that renders the OTA update prompts driven by DashViewModel.update. */
@Composable
fun UpdateDialogHost(vm: DashViewModel) {
    val s by vm.update.collectAsStateWithLifecycle()

    // Returning from the "install unknown apps" settings screen -> retry.
    val permLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.StartActivityForResult()
    ) { vm.downloadAndInstall() }

    when (s.phase) {
        UpdatePhase.AVAILABLE -> AlertDialog(
            onDismissRequest = { vm.dismissUpdate() },
            title = { Text("Update available") },
            text = {
                Text(
                    "Version ${s.versionName} is available." +
                        if (s.notes.isNotBlank()) "\n\n${s.notes}" else ""
                )
            },
            confirmButton = { TextButton(onClick = { vm.downloadAndInstall() }) { Text("Update") } },
            dismissButton = { TextButton(onClick = { vm.dismissUpdate() }) { Text("Later") } },
        )

        UpdatePhase.NEED_PERMISSION -> AlertDialog(
            onDismissRequest = { vm.dismissUpdate() },
            title = { Text("Allow installs") },
            text = { Text("To install updates, allow RaceDash to install apps, then tap Update.") },
            confirmButton = {
                TextButton(onClick = { permLauncher.launch(vm.installPermissionIntent()) }) {
                    Text("Open settings")
                }
            },
            dismissButton = { TextButton(onClick = { vm.dismissUpdate() }) { Text("Cancel") } },
        )

        UpdatePhase.DOWNLOADING -> AlertDialog(
            onDismissRequest = { },
            title = { Text("Downloading ${s.versionName}") },
            text = {
                Column {
                    LinearProgressIndicator(
                        progress = { s.progress / 100f },
                        modifier = Modifier.fillMaxWidth(),
                    )
                    Spacer(Modifier.height(10.dp))
                    Text("${s.progress}%")
                }
            },
            confirmButton = { },
        )

        UpdatePhase.UPTODATE -> AlertDialog(
            onDismissRequest = { vm.dismissUpdate() },
            title = { Text("Up to date") },
            text = { Text("You're on the latest version (${s.versionName}).") },
            confirmButton = { TextButton(onClick = { vm.dismissUpdate() }) { Text("OK") } },
        )

        UpdatePhase.ERROR -> AlertDialog(
            onDismissRequest = { vm.dismissUpdate() },
            title = { Text("Update error") },
            text = { Text(s.message.ifBlank { "Couldn't check for updates." }) },
            confirmButton = { TextButton(onClick = { vm.dismissUpdate() }) { Text("OK") } },
        )

        else -> { /* IDLE, CHECKING, INSTALLING -> no dialog */ }
    }
}
