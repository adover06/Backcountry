import { initializeApp } from "firebase/app";
import { GoogleAuthProvider, getAuth } from "firebase/auth";

const firebaseConfig = {
  apiKey: import.meta.env.VITE_FIREBASE_API_KEY,
  authDomain: import.meta.env.VITE_FIREBASE_AUTH_DOMAIN,
  projectId: import.meta.env.VITE_FIREBASE_PROJECT_ID,
  appId: import.meta.env.VITE_FIREBASE_APP_ID,
};

// Firebase is optional. Discovery is public, so an unconfigured deployment must
// still boot — `initializeApp` with an empty key throws `auth/invalid-api-key` at
// module load, which previously took the entire app down to a blank page.
export const firebaseEnabled = Boolean(firebaseConfig.apiKey && firebaseConfig.projectId);

let firebaseApp = null;
let auth = null;
let googleProvider = null;

if (firebaseEnabled) {
  try {
    firebaseApp = initializeApp(firebaseConfig);
    auth = getAuth(firebaseApp);
    googleProvider = new GoogleAuthProvider();
  } catch (error) {
    // A malformed config should degrade to signed-out, never to a white screen.
    console.warn("Firebase failed to initialize; continuing signed out.", error);
    firebaseApp = null;
    auth = null;
    googleProvider = null;
  }
}

export { firebaseApp, auth, googleProvider };
