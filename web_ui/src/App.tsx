import { Routes, Route } from "react-router-dom";
import { AuthProvider, useAuth } from "@/auth/TokenContext";
import { ToastProvider } from "@/components/Toast";
import Layout from "@/components/Layout";
import LoginScreen from "@/components/LoginScreen";
import Dashboard from "@/pages/Dashboard";
import Incidents from "@/pages/Incidents";
import Alerts from "@/pages/Alerts";
import Settings from "@/pages/Settings";
import Logs from "@/pages/Logs";

function Shell() {
  const { authed } = useAuth();
  if (!authed) return <LoginScreen />;
  return (
    <Layout>
      <Routes>
        <Route path="/" element={<Dashboard />} />
        <Route path="/incidents" element={<Incidents />} />
        <Route path="/alerts" element={<Alerts />} />
        <Route path="/settings" element={<Settings />} />
        <Route path="/logs" element={<Logs />} />
        <Route path="*" element={<Dashboard />} />
      </Routes>
    </Layout>
  );
}

export default function App() {
  return (
    <AuthProvider>
      <ToastProvider>
        <Shell />
      </ToastProvider>
    </AuthProvider>
  );
}
