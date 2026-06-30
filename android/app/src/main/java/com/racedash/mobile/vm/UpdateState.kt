package com.racedash.mobile.vm

enum class UpdatePhase {
    IDLE,            // nothing to show
    CHECKING,        // querying the manifest
    AVAILABLE,       // newer version found -> prompt
    UPTODATE,        // manual check, already current
    NEED_PERMISSION, // user must allow "install unknown apps"
    DOWNLOADING,     // fetching the APK
    INSTALLING,      // handed off to the system installer
    ERROR,           // couldn't check/download
}

data class UpdateState(
    val phase: UpdatePhase = UpdatePhase.IDLE,
    val versionName: String = "",
    val notes: String = "",
    val progress: Int = 0,
    val message: String = "",
)
