package dev.realm.terminal

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject

/** Save endpoint metadata only. Never persist bearer tokens or Telnet gates. */
object ProfileStore {
    private const val PREFS = "realm_endpoint_profiles_v1"
    private const val KEY = "profiles"

    fun load(context: Context): List<HostProfile> {
        val source = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getString(KEY, null)
            ?: return emptyList()
        return try {
            val array = JSONArray(source)
            (0 until minOf(array.length(), 20)).mapNotNull { index ->
                try {
                    val data = array.getJSONObject(index)
                    HostProfile(
                        data.getString("label"), data.getString("host"), data.getInt("port"),
                        TransportMode.valueOf(data.getString("mode")),
                        data.optString("worldId", ""),
                    ).also { it.validate(allowEmulatorBridge = false) }
                } catch (_: Exception) { null }
            }
        } catch (_: Exception) { emptyList() }
    }

    fun save(context: Context, profiles: List<HostProfile>) {
        require(profiles.size <= 20)
        val array = JSONArray()
        for (profile in profiles) {
            profile.validate(allowEmulatorBridge = false)
            array.put(JSONObject()
                .put("label", profile.label)
                .put("host", profile.host)
                .put("port", profile.port)
                .put("mode", profile.mode.name)
                .put("worldId", profile.worldId))
        }
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit()
            .putString(KEY, array.toString()).apply()
    }
}
