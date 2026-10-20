import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose")
}
// Firebase is optional until the project configuration is supplied.
val pushConfigured = file("google-services.json").isFile
if (pushConfigured) apply(plugin = "com.google.gms.google-services")
val releaseSigningFile = rootProject.file("../data/android-release/signing.properties")
val releaseSigning = Properties().apply {
    if (releaseSigningFile.isFile) releaseSigningFile.inputStream().use { load(it) }
}
// Embedded in the APK at build time; never editable through the app.
val viewerCredentials = Properties().apply {
    val source = rootProject.file("../data/android-viewer.env")
    check(source.isFile) { "Missing embedded app credential: data/android-viewer.env" }
    source.inputStream().use { load(it) }
}
val appToken = viewerCredentials.getProperty("KAIFORWARD_READ_TOKEN")
check(!appToken.isNullOrBlank() && appToken.all { it.code in 33..126 }) {
    "KAIFORWARD_READ_TOKEN must be a nonempty printable ASCII token without spaces"
}
fun quoted(value: String) = "\"" + value.replace("\\", "\\\\").replace("\"", "\\\"") + "\""
android {
    namespace = "dev.kaiforward.app"
    compileSdk = 36
    defaultConfig {
        applicationId = "dev.kaiforward.app"
        minSdk = 26
        targetSdk = 36
        versionCode = 9
        versionName = "0.5.0"
        buildConfigField("boolean", "PUSH_CONFIGURED", pushConfigured.toString())
        buildConfigField("String", "RECEIVER_URL", quoted("https://peaceful-cuttlefish-556.convex.site"))
        buildConfigField("String", "SMS_URL", quoted("https://nokia8110.tailcf48c.ts.net"))
        buildConfigField("String", "APP_TOKEN", quoted(appToken))
        testInstrumentationRunner = "dev.kaiforward.app.DeviceSmokeRunner"
    }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    buildFeatures { compose = true; buildConfig = true }
    signingConfigs {
        if (releaseSigningFile.isFile) create("release") {
            storeFile = rootProject.file(releaseSigning.getProperty("storeFile"))
            storePassword = releaseSigning.getProperty("storePassword")
            keyAlias = releaseSigning.getProperty("keyAlias")
            keyPassword = releaseSigning.getProperty("keyPassword")
            storeType = "PKCS12"
        }
    }
    buildTypes {
        release {
            isDebuggable = false
            isMinifyEnabled = true
            isShrinkResources = true
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"))
            signingConfig = signingConfigs.findByName("release")
        }
    }
}
tasks.matching { it.name == "preReleaseBuild" }.configureEach {
    doFirst { check(releaseSigningFile.isFile) { "Release signing credentials are missing: data/android-release/signing.properties" } }
}
kotlin { compilerOptions { jvmTarget.set(org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17) } }
dependencies {
    implementation("com.google.firebase:firebase-messaging:25.0.1")
    implementation("androidx.work:work-runtime-ktx:2.10.5")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-play-services:1.10.2")
    implementation("com.squareup.okhttp3:okhttp:4.12.0")
    implementation(platform("androidx.compose:compose-bom:2025.11.00"))
    implementation("androidx.activity:activity-compose:1.11.0")
    implementation("androidx.lifecycle:lifecycle-viewmodel-compose:2.9.4")
    implementation("androidx.lifecycle:lifecycle-runtime-compose:2.9.4")
    implementation("androidx.compose.material3:material3")
    implementation("androidx.compose.material:material-icons-core")
    implementation("androidx.compose.ui:ui-tooling-preview")
    debugImplementation("androidx.compose.ui:ui-tooling")
    testImplementation("org.json:json:20240303")
    testImplementation("junit:junit:4.13.2")
    testImplementation("org.jetbrains.kotlinx:kotlinx-coroutines-test:1.10.2")
}
