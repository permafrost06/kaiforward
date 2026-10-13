/* SPDX-License-Identifier: GPL-2.0-only */
/* Included by q6voice.c after its native helpers and common session state. */
static bool kaiforward_playback_session(uint16_t port_id, uint32_t session_id)
{
	if (port_id == VOICE_PLAYBACK_TX)
		return session_id == VOICE_SESSION_VSID ||
		       session_id == VOLTE_SESSION_VSID ||
		       session_id == VOWLAN_SESSION_VSID ||
		       session_id == VOICEMMODE1_VSID;
	if (port_id == VOICE2_PLAYBACK_TX)
		return session_id == VOICE2_SESSION_VSID ||
		       session_id == VOICEMMODE2_VSID;
	return false;
}

int voc_start_playback(uint32_t set, uint16_t port_id)
{
	struct voice_data *v;
	int index, rc, ret = 0;
	bool active = false;

	if (set > 1 || (port_id != VOICE_PLAYBACK_TX &&
		       port_id != VOICE2_PLAYBACK_TX))
		return -EINVAL;

	for (index = 0; index < MAX_VOC_SESSIONS; index++) {
		v = &common.voice[index];
		if (!kaiforward_playback_session(port_id, v->session_id))
			continue;

		mutex_lock(&v->lock);
		if (set && v->music_info.count == ~0U) {
			if (!ret)
				ret = -EOVERFLOW;
			mutex_unlock(&v->lock);
			continue;
		}

		v->music_info.port_id = port_id;
		if (set)
			v->music_info.count++;
		else if (v->music_info.count)
			v->music_info.count--;
		v->music_info.play_enable = v->music_info.count != 0;

		/* Keep pending state for handover, as the original path does. */
		if (voice_get_cvs_handle(v)) {
			active = true;
			rc = set ? voice_cvs_start_playback(v) :
				   voice_cvs_stop_playback(v);
			pr_info("kaiforward: incall playback vsid=%#x port=%#x set=%u count=%u rc=%d\n",
				v->session_id, port_id, set,
				v->music_info.count, rc);
			if (rc < 0 && !ret)
				ret = rc;
		}
		mutex_unlock(&v->lock);
	}

	if (set && !active && !ret) {
		pr_warn("kaiforward: no active incall playback session for port=%#x\n", port_id);
		ret = -ENODEV;
	}
	return ret;
}
