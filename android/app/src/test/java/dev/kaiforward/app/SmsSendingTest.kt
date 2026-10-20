package dev.kaiforward.app

import org.junit.Assert.*
import org.junit.Test
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.advanceTimeBy
import okhttp3.Request
import okio.Buffer
import org.json.JSONObject
import java.io.IOException

@OptIn(kotlinx.coroutines.ExperimentalCoroutinesApi::class)
class SmsTransportTest {
    private fun response(code: Int, body: String) = SmsResponse(code, JSONObject(body))
    private fun slots() = response(200, """{"slots":[1,2]}""")
    private fun job(status: String, code: Int = 200) = response(code, """{"job":42,"slot":1,"status":"$status"}""")

    @Test fun sendsOnceAndFollowsDirectPhoneStatus() = runTest {
        val requests = mutableListOf<Request>()
        var sent = 0
        val sender = SmsSending(this, { sent++ }, exchange = { request ->
            requests += request
            assertEquals("Bearer app-token", request.header("Authorization"))
            when (request.url.encodedPath) {
                "/v1/sms" -> slots()
                "/v1/sms/send" -> job("queued", 202)
                else -> job("sent")
            }
        })
        sender.connect("https://nokia.example", "app-token"); runCurrent()
        sender.send("123", "Hello", 1)
        sender.send("123", "Hello", 1)
        runCurrent()
        assertEquals(1, requests.count { it.method == "POST" })
        val buffer = Buffer(); requests.first { it.method == "POST" }.body!!.writeTo(buffer)
        val payload = JSONObject(buffer.readUtf8())
        assertEquals("123", payload.getString("number"))
        assertEquals("Hello", payload.getString("text"))
        assertEquals(36, payload.getString("requestId").length)
        assertEquals(1L, sender.state.value.accepted)
        assertEquals(1, sent)
        assertTrue(sender.state.value.notice.startsWith("Sent"))
        sender.close()
    }
    @Test fun lostSubmissionRecoversByReadingItsIdWithoutResending() = runTest {
        val requests = mutableListOf<Request>()
        var savedId = ""
        val sender = SmsSending(this, {}, exchange = { request ->
            requests += request
            when {
                request.method == "POST" -> throw IOException("response lost")
                request.url.encodedPath == "/v1/sms" -> slots()
                else -> job("sent")
            }
        }, onTrack = { id, _ -> savedId = id })
        sender.connect("https://nokia.example", "app-token"); runCurrent()
        sender.send("123", "Hello", 1); runCurrent()
        assertTrue(sender.state.value.notice.contains("unknown"))
        assertFalse(sender.state.value.online)
        assertEquals(0L, sender.state.value.accepted)
        sender.connect("https://nokia.example", "app-token"); runCurrent()
        assertEquals(1, requests.count { it.method == "POST" })
        assertTrue(requests.any { it.url.encodedPath == "/v1/sms/requests/$savedId" })
        assertEquals(1L, sender.state.value.accepted)
        sender.close()
    }
    @Test fun restoredRequestNeedsNoMessageOrSecondPost() = runTest {
        val requests = mutableListOf<Request>()
        val sender = SmsSending(this, {}, exchange = { request ->
            requests += request
            if (request.url.encodedPath == "/v1/sms") slots() else job("sent")
        }, restoredRequest = "a2fb625a-4599-4bbb-9358-e46cf9c39af5" to 1)
        sender.connect("https://nokia.example", "app-token"); runCurrent()
        assertTrue(requests.all { it.method == "GET" })
        assertEquals(42L, sender.state.value.job)
        sender.close()
    }
    @Test fun pollingPausesInBackgroundAndDoesNotResubmitOnResume() = runTest {
        var polls = 0
        var posts = 0
        var finished = false
        val sender = SmsSending(this, {}, exchange = { request ->
            when {
                request.method == "POST" -> { posts++; job("queued", 202) }
                request.url.encodedPath == "/v1/sms" -> slots()
                else -> { polls++; job(if (finished) "sent" else "queued") }
            }
        })
        sender.connect("https://nokia.example", "app-token"); runCurrent()
        sender.send("123", "Hello", 1); runCurrent()
        val before = polls
        sender.setActive(false); advanceTimeBy(10_000); runCurrent()
        assertEquals(before, polls)
        finished = true
        sender.setActive(true); runCurrent()
        assertEquals(before + 1, polls)
        assertEquals(1, posts)
        sender.close()
    }
    @Test fun disconnectedSubmissionCannotReplaceNewConnectionState() = runTest {
        val pending = CompletableDeferred<SmsResponse>()
        val sender = SmsSending(this, {}, exchange = { request ->
            if (request.method == "POST") pending.await() else slots()
        })
        sender.connect("https://nokia.example", "app-token"); runCurrent()
        sender.send("123", "Hello", 1); runCurrent()
        sender.disconnect()
        sender.connect("https://other.example", "app-token"); runCurrent()
        pending.complete(job("queued", 202)); runCurrent()
        assertEquals("https://other.example", sender.state.value.endpoint)
        assertEquals(0L, sender.state.value.accepted)
        assertNull(sender.state.value.job)
        sender.close()
    }
    @Test fun missingRequestCanBeRetriedExplicitlyWithTheSameId() = runTest {
        val ids = mutableListOf<String>()
        val sender = SmsSending(this, {}, exchange = { request ->
            when {
                request.method == "POST" -> {
                    val buffer = Buffer(); request.body!!.writeTo(buffer)
                    ids += JSONObject(buffer.readUtf8()).getString("requestId")
                    if (ids.size == 1) throw IOException("request never arrived") else job("sent", 202)
                }
                request.url.encodedPath == "/v1/sms" -> slots()
                else -> response(404, "{}")
            }
        })
        sender.connect("https://nokia.example", "app-token"); runCurrent()
        sender.send("123", "Hello", 1); runCurrent()
        sender.connect("https://nokia.example", "app-token"); runCurrent()
        assertTrue(sender.state.value.online)
        assertEquals(1, ids.size)
        assertTrue(sender.state.value.notice.contains("No queued SMS"))
        sender.send("123", "Hello", 1); runCurrent()
        assertEquals(2, ids.size)
        assertEquals(ids[0], ids[1])
        assertEquals(1L, sender.state.value.accepted)
        sender.close()
    }
    @Test fun conflictingLateRequestKeepsDraftAndRecoversOriginalStatus() = runTest {
        var posts = 0
        var found = false
        val sender = SmsSending(this, {}, exchange = { request ->
            when {
                request.method == "POST" -> {
                    posts++
                    if (posts == 1) throw IOException("response lost") else response(409, "{}")
                }
                request.url.encodedPath == "/v1/sms" -> slots()
                found -> job("sent")
                else -> response(404, "{}")
            }
        })
        sender.connect("https://nokia.example", "app-token"); runCurrent()
        sender.send("123", "Hello", 1); runCurrent()
        val id = sender.state.value.requestId
        sender.connect("https://nokia.example", "app-token"); runCurrent()
        sender.send("123", "Changed text", 2); runCurrent()
        assertEquals(id, sender.state.value.requestId)
        assertEquals(0L, sender.state.value.accepted)
        found = true
        sender.connect("https://nokia.example", "app-token"); runCurrent()
        assertEquals(42L, sender.state.value.job)
        assertEquals(0L, sender.state.value.accepted)
        assertEquals(2, posts)
        assertEquals(1, sender.state.value.slot)
        sender.close()
    }
    @Test fun deniedProbeNeverEnablesSending() = runTest {
        val sender = SmsSending(this, {}, exchange = { response(401, "{}") })
        sender.connect("https://nokia.example", "app-token"); runCurrent()
        assertFalse(sender.state.value.online)
        assertTrue(sender.state.value.notice.contains("denied"))
        sender.close()
    }
}

class SmsSendingTest {
    @Test fun gsmExtensionsUseTwoSeptets() {
        assertTrue(smsLength("a".repeat(160)).valid)
        assertFalse(smsLength("a".repeat(161)).valid)
        assertTrue(smsLength("€".repeat(80)).valid)
        assertFalse(smsLength("€".repeat(81)).valid)
        assertEquals(2, smsLength("^").used)
    }
    @Test fun unicodeUsesUtf16Units() {
        assertTrue(smsLength("বা".repeat(35)).valid)
        assertFalse(smsLength("বা".repeat(35) + "x").valid)
        assertTrue(smsLength("😀".repeat(35)).valid)
        assertFalse(smsLength("😀".repeat(36)).valid)
        assertEquals(70, smsLength("tab\t").limit)
        for (text in listOf("", "a\u0000b", "a\u001bb", "\uD800", "\uDC00")) assertFalse(smsLength(text).valid)
    }
    @Test fun recipientsAreTelephoneNumbersAndFormattingIsRemoved() {
        assertEquals("+8801712345678", smsNumber(" +880 (17) 1234-5678 "))
        for (number in listOf("123", "+880 (17) 1234-5678", "1".repeat(20))) assertTrue(validSmsNumber(number))
        for (number in listOf("12", "1".repeat(21), "*123#", "Bank", "১২৩", "123\n2", "+12+34")) assertFalse(validSmsNumber(number))
    }
}
