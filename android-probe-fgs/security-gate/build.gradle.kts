plugins {
    id("com.android.library")
}

android {
    namespace = "de.prodbykesti.cudysecurity"
    compileSdk = 35

    defaultConfig {
        minSdk = 23
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}

dependencies {
    api("androidx.biometric:biometric:1.1.0")
}
