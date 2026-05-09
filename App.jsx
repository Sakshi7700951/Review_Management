import { Routes, Route, Navigate } from 'react-router-dom';
import { AppProvider } from './context/AppContext';
import Dashboard from './pages/PatientDashboard';
import GlobalDashboard from './pages/GlobalDashboard';
import ReviewHub from './pages/ReviewHub';
import Analytics from './pages/Analytics';
import AutomationLogs from './pages/AutomationLogs';
import ReputationScorecard from './pages/ReputationScorecard';

export default function App() {
  return (
    <AppProvider>
      <Routes>
        <Route path="/dashboard" element={<GlobalDashboard />} />
        <Route path="/" element={<Dashboard />} />
        <Route path="/review-hub" element={<ReviewHub />} />
        <Route path="/analytics" element={<Analytics />} />
        <Route path="/reputation-scorecard" element={<ReputationScorecard />} />
        <Route path="/automation" element={<AutomationLogs />} />
        <Route path="*" element={<Navigate to="/dashboard" replace />} />
      </Routes>
    </AppProvider>
  );
}