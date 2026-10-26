plugins {
    id("com.android.application")
}

android {
    namespace = "de.prodbykesti.cudyapp"
    compileSdk = 35

    defaultConfig {
        applicationId = "de.prodbykesti.cudyapp"
        minSdk = 23
        targetSdk = 35
        versionCode = 1
        versionName = "0.1.0-alpha"
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}

dependencies {
    implementation(project(":security-gate"))
}
