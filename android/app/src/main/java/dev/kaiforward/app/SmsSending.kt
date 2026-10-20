package dev.kaiforward.app

import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.MediaType.Companion.toMediaType
import org.json.JSONObject
import java.util.UUID
import java.util.concurrent.TimeUnit

private const val GSM_BASIC = "@£\$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
private const val GSM_ESCAPED = "^{}\\[~]|€"

data class SmsLength(val used: Int, val limit: Int, val alphabet: String, val valid: Boolean)
fun smsLength(text: String): SmsLength {
    val gsm = text.all { it in GSM_BASIC || it in GSM_ESCAPED }
    val used = if (gsm) text.sumOf { if (it in GSM_ESCAPED) 2 else 1 } else text.length
    val limit = if (gsm) 160 else 70
    var validUnicode = true
    var i = 0
    while (i < text.length) {
        val c = text[i++]
        if (c.isHighSurrogate()) {
            if (i == text.length || !text[i++].isLowSurrogate()) validUnicode = false
        } else if (c.isLowSurrogate()) validUnicode = false
    }
    return SmsLength(used, limit, if (gsm) "GSM" else "UTF-16", text.isNotEmpty() && used <= limit && validUnicode &&
        text.none { it.code < 32 && it !in "\n\r\t" })
}
fun smsNumber(number: String): String = number.trim().filterNot { it in " ()-" }
fun validSmsNumber(number: String): Boolean = Regex("\\+?[0-9]{3,20}").matches(smsNumber(number))

data class SendingState(
    val endpoint: String = "",
    val online: Boolean = false,
    val readySlots: Set<Int> = emptySet(),
    val busy: Boolean = false,
    val notice: String = "Configure SMS sending in Connection settings.",
    val job: Long? = null,
    val slot: Int = 1,
    val accepted: Long = 0,
    val requestId: String? = null,
)
data class SmsResponse(val status: Int, val body: JSONObject)

/** POSTs once. Durable request IDs allow safe status recovery after connection/process loss. */
class SmsSending(
    private val scope: CoroutineScope,
    private val onSent: () -> Unit,
    private val exchange: (suspend (Request) -> SmsResponse)? = null,
    private val onTrack: (String, Int) -> Unit = { _, _ -> },
    private val onForget: () -> Unit = {},
    restoredRequest: Pair<String, Int>? = null,
) {
    private val mutable = MutableStateFlow(SendingState(requestId = restoredRequest?.first, slot = restoredRequest?.second ?: 1))
    val state = mutable.asStateFlow()
    private val client = OkHttpClient.Builder().connectTimeout(10, TimeUnit.SECONDS)
        .readTimeout(10, TimeUnit.SECONDS).writeTimeout(10, TimeUnit.SECONDS)
        .retryOnConnectionFailure(false).followRedirects(false).followSslRedirects(false).build()
    private var token = ""
    private var generation = 0L
    private var connectJob: Job? = null
    private var submitJob: Job? = null
    private var pollJob: Job? = null
    private var active = true
    private var terminal = false
    private var missingRequest = false
    private var preserveDraft = false

    private suspend fun request(path: String, body: JSONObject? = null): SmsResponse {
        val builder = Request.Builder().url(mutable.value.endpoint + path)
            .header("Authorization", "Bearer $token").header("Accept", "application/json")
        if (body != null) builder.post(body.toString().toRequestBody("application/json; charset=utf-8".toMediaType()))
        val request = builder.build()
        return exchange?.invoke(request) ?: withContext(Dispatchers.IO) {
            client.newCall(request).execute().use { response ->
                SmsResponse(response.code, runCatching { JSONObject(response.body?.string().orEmpty()) }.getOrDefault(JSONObject()))
            }
        }
    }

    fun connect(endpoint: String, token: String) {
        if (mutable.value.busy) return
        stop()
        this.token = token
        terminal = false
        val version = generation
        mutable.value = mutable.value.copy(endpoint = endpoint, online = false, readySlots = emptySet(), notice = "Connecting to your KaiForward phone…")
        connectJob = scope.launch {
            try {
                val response = request("/v1/sms")
                if (version != generation) return@launch
                checkResponse(response)
                val slots = response.body.getJSONArray("slots")
                val ready = (0 until slots.length()).map { slots.getInt(it) }.filter { it in 1..2 }.toSet()
                check(ready.isNotEmpty())
                mutable.value = mutable.value.copy(online = true, readySlots = ready,
                    notice = if (mutable.value.requestId == null) "Ready to send through your KaiForward phone." else "Checking the last SMS request…")
                poll()
            } catch (error: CancellationException) { throw error }
            catch (error: Exception) {
                if (version == generation) lost(errorText(error))
            }
        }
    }

    fun send(number: String, text: String, slot: Int) {
        val current = mutable.value
        if (current.busy || !current.online || slot !in current.readySlots || !validSmsNumber(number) || !smsLength(text).valid) return
        pollJob?.cancel()
        val recovering = missingRequest && current.job == null && current.requestId != null
        val id = if (recovering) current.requestId!! else UUID.randomUUID().toString()
        missingRequest = false
        preserveDraft = false
        // Save only the request ID and SIM before sending, never recipient or text.
        try { onTrack(id, slot) } catch (_: Exception) {
            mutable.value = current.copy(notice = "Cannot save SMS tracking. Free some storage and try again.")
            return
        }
        terminal = false
        val version = generation
        mutable.value = current.copy(busy = true, job = null, requestId = id, slot = slot, notice = "Waiting for the phone…")
        submitJob = scope.launch {
            try {
                val response = request("/v1/sms/send", JSONObject().put("requestId", id).put("slot", slot)
                    .put("number", smsNumber(number)).put("text", text))
                if (version != generation) return@launch
                if (recovering && response.status == 409) {
                    preserveDraft = true
                    mutable.value = mutable.value.copy(busy = false, online = false, readySlots = emptySet(),
                        notice = "The earlier request contains different text. Reconnect to check it; your draft is kept.")
                    return@launch
                }
                if (response.status in setOf(400, 401, 403, 404, 409, 413, 415, 503)) {
                    onForget()
                    terminal = true
                    val denied = response.status in setOf(401, 403, 404, 503)
                    mutable.value = mutable.value.copy(busy = false, requestId = null,
                        online = !denied, readySlots = if (denied) emptySet() else mutable.value.readySlots,
                        notice = "SMS request was rejected. " + responseError(response.status))
                    return@launch
                }
                if (response.status != 200 && response.status != 202) {
                    checkResponse(response)
                    error("The phone did not acknowledge this SMS request.")
                }
                jobStatus(response.body)
                poll()
            } catch (error: CancellationException) { throw error }
            catch (error: Exception) {
                if (version == generation) {
                    mutable.value = mutable.value.copy(busy = false, online = false, readySlots = emptySet(),
                        notice = "SMS result unknown. Reconnect to check this request before sending again. " + errorText(error))
                }
            }
        }
    }

    private fun jobStatus(body: JSONObject) {
        val current = mutable.value
        val status = body.getString("status")
        val slot = body.getInt("slot")
        check(slot in 1..2)
        val job = body.getLong("job")
        check(job > 0)
        val notice = when (status) {
            "queued" -> "Queued on SIM $slot; waiting for the SIM to be ready."
            "submitting" -> "Sending from SIM $slot…"
            "sent" -> "Sent from SIM $slot. Pull down to refresh message history."
            "failed" -> "SMS failed on SIM $slot."
            "uncertain" -> "SMS result unknown. Check with the recipient before sending again."
            else -> error("Invalid SMS job status.")
        }
        missingRequest = false
        terminal = status in setOf("sent", "failed", "uncertain")
        mutable.value = current.copy(busy = false, job = job, slot = slot, notice = notice,
            accepted = current.accepted + if (current.job == null && !preserveDraft) 1 else 0)
        if (status == "sent" && notice != current.notice) onSent()
    }

    private fun poll() {
        pollJob?.cancel()
        val id = mutable.value.requestId ?: return
        if (!active || terminal || !mutable.value.online || mutable.value.busy) return
        val version = generation
        pollJob = scope.launch {
            while (active && !terminal && version == generation) {
                try {
                    val response = request("/v1/sms/requests/$id")
                    if (version != generation || mutable.value.requestId != id) return@launch
                    if (response.status == 404 && mutable.value.job == null) {
                        missingRequest = true
                        terminal = true
                        mutable.value = mutable.value.copy(notice = "No queued SMS found. Tap Send again to retry safely.")
                        return@launch
                    }
                    checkResponse(response)
                    jobStatus(response.body)
                } catch (error: CancellationException) { throw error }
                catch (error: Exception) {
                    if (version == generation) lost("Could not check the last SMS request. " + errorText(error))
                    return@launch
                }
                if (!terminal) delay(2_000)
            }
        }
    }

    fun setActive(value: Boolean) {
        active = value
        if (value) poll() else pollJob?.cancel()
    }
    private fun responseError(status: Int) = when (status) {
        401, 403 -> "SMS access denied. Set the Nokia’s app_token to the same token used by this app."
        404 -> "SMS endpoint or request not found. Check that the Nokia has the updated firmware."
        503 -> "SMS access is not configured. Set app_token on the Nokia."
        400, 409, 413, 415 -> "The Nokia refused this SMS request."
        else -> "The Nokia returned HTTP $status."
    }
    private fun checkResponse(response: SmsResponse) {
        if (response.status !in setOf(200, 202)) error(responseError(response.status))
    }
    private fun errorText(error: Exception): String = when (error) {
        is java.net.UnknownHostException -> "Cannot resolve the Nokia’s address. Check Tailscale DNS."
        is java.net.SocketTimeoutException -> "The Nokia took too long to respond. Check Tailscale and reconnect."
        is javax.net.ssl.SSLException -> "Cannot verify the Nokia’s HTTPS connection. Check its Tailscale HTTPS setup."
        is org.json.JSONException -> "The Nokia returned an invalid SMS response. Check its firmware and HTTPS URL."
        is java.io.IOException -> "Connection to the Nokia was interrupted. Check Tailscale and reconnect."
        is IllegalStateException -> error.message ?: "Invalid phone response."
        is IllegalArgumentException -> "Invalid Nokia URL or app token. Check Connection settings."
        else -> "Unable to connect to the Nokia. Check Tailscale and the HTTPS address."
    }
    private fun lost(reason: String) {
        mutable.value = mutable.value.copy(online = false, readySlots = emptySet(), busy = false, notice = reason)
    }
    private fun stop() { generation++; connectJob?.cancel(); submitJob?.cancel(); pollJob?.cancel() }
    fun disconnect() { stop(); token = ""; terminal = false; missingRequest = false; preserveDraft = false; mutable.value = SendingState() }
    fun close() { stop(); client.dispatcher.cancelAll(); client.dispatcher.executorService.shutdown(); client.connectionPool.evictAll() }
}
