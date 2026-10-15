package dev.kaiforward.app

import java.time.Instant
import java.time.ZoneId
import java.util.Locale
import org.junit.Assert.assertEquals
import org.junit.Test

class MessageTimeTest {
    private val zone = ZoneId.of("Asia/Dhaka")
    private val now = Instant.parse("2026-10-14T18:05:00Z") // October 15, 00:05 locally.

    private fun label(timestamp: String) = updatedLabel(Instant.parse(timestamp).toEpochMilli(), now, zone, Locale.UK)

    @Test fun todayShowsOnlyTheLocalTime() {
        assertEquals("Updated 00:01", label("2026-10-14T18:01:00Z"))
    }

    @Test fun yesterdayUsesTheLocalCalendarDateEvenJustAfterMidnight() {
        assertEquals("Updated yesterday", label("2026-10-14T17:59:00Z"))
    }

    @Test fun olderUpdatesShowOnlyTheDate() {
        assertEquals("Updated 13 Oct 2026", label("2026-10-13T12:00:00Z"))
    }
}
