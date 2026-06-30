package com.racedash.mobile.data

import android.content.Context
import java.io.File

/**
 * Tracks recorded session files on disk and where they go after upload.
 *   <externalFilesDir>/sessions/   - recorded + pending-upload
 *   <externalFilesDir>/uploaded/   - successfully uploaded (kept, not deleted)
 */
class SessionStore(context: Context) {
    private val root = context.getExternalFilesDir(null)
    val sessionsDir = File(root, "sessions")
    val uploadedDir = File(root, "uploaded")

    /** Pending .ndjson files (optionally excluding the currently-recording one). */
    fun pending(exclude: File?): List<File> {
        val list = sessionsDir.listFiles { f ->
            f.isFile && f.name.endsWith(".ndjson") &&
                (exclude == null || f.absolutePath != exclude.absolutePath)
        }?.toList() ?: emptyList()
        return list.sortedBy { it.name }
    }

    /** Move a file to /uploaded after a successful POST. */
    fun markUploaded(f: File): Boolean {
        uploadedDir.mkdirs()
        return f.renameTo(File(uploadedDir, f.name))
    }
}
