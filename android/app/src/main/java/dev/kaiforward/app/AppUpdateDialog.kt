package dev.kaiforward.app

import android.content.Intent
import android.net.Uri
import android.provider.Settings
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.ui.platform.LocalContext

@Composable
internal fun AppUpdateDialog(state: UpdateState, model: AppUpdateViewModel) {
    val context = LocalContext.current
    val permission = rememberLauncherForActivityResult(ActivityResultContracts.StartActivityForResult()) {
        if (context.packageManager.canRequestPackageInstalls()) model.install()
    }
    val update = state.update ?: return
    if (!state.visible || !state.ready) return
    AlertDialog(onDismissRequest = model::dismiss,
        title = { Text("Install KaiForward ${update.versionName}?") },
        text = { Text("The update has been downloaded and verified. It’s ready to install.") },
        confirmButton = {
            TextButton(enabled = !state.busy, onClick = {
                if (context.packageManager.canRequestPackageInstalls()) model.install()
                else permission.launch(Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                    Uri.parse("package:${context.packageName}")))
            }) { Text("Install") }
        },
        dismissButton = { TextButton(onClick = model::dismiss) { Text("Later") } })
}
