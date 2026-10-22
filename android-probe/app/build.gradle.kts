plugins {
    id("com.android.application")
}

android {
    namespace = "de.prodbykesti.cudyrouterprobe"
    compileSdk = 35

    defaultConfig {
        applicationId = "de.prodbykesti.cudyrouterprobe"
        minSdk = 23
        targetSdk = 35
        versionCode = 1
        versionName = "0.1"
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}
