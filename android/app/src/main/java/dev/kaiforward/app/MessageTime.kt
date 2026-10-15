package dev.kaiforward.app

import java.time.Instant
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import java.time.format.FormatStyle
import java.util.Locale

fun updatedLabel(
    timestamp: Long,
    now: Instant = Instant.now(),
    zone: ZoneId = ZoneId.systemDefault(),
    locale: Locale = Locale.getDefault(),
): String {
    val updated = Instant.ofEpochMilli(timestamp).atZone(zone)
    val today = now.atZone(zone).toLocalDate()
    val value = when (updated.toLocalDate()) {
        today -> updated.format(DateTimeFormatter.ofLocalizedTime(FormatStyle.SHORT).withLocale(locale))
        today.minusDays(1) -> "yesterday"
        else -> updated.format(DateTimeFormatter.ofLocalizedDate(FormatStyle.MEDIUM).withLocale(locale))
    }
    return "Updated $value"
}
