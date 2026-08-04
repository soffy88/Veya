'use client';

import { useState, useEffect } from 'react';
import { Card, CardHeader, CardContent, CardTitle } from '@helios/oui';
import { Button, Badge } from '@helios/oui';

interface AuditLog {
  timestamp: string;
  user: string;
  action: string;
  resource: string;
  success: boolean;
  details: Record<string, any>;
}

interface AuditStats {
  total_actions: number;
  successful_actions: number;
  failed_actions: number;
  success_rate: number;
  unique_users: number;
  users: string[];
  top_actions: { action: string; count: number }[];
}

export default function SecurityPage() {
  const [logs, setLogs] = useState<AuditLog[]>([]);
  const [stats, setStats] = useState<AuditStats | null>(null);
  const [loading, setLoading] = useState(true);
  const [filter, setFilter] = useState({
    user: '',
    action: '',
    limit: 100
  });

  useEffect(() => {
    loadAuditData();
  }, []);

  async function loadAuditData() {
    try {
      const [logsRes, statsRes] = await Promise.all([
        fetch(`http://localhost:8000/security/audit-logs?limit=${filter.limit}`),
        fetch('http://localhost:8000/security/audit-stats')
      ]);
      
      const logsData = await logsRes.json();
      const statsData = await statsRes.json();
      
      setLogs(logsData.logs || []);
      setStats(statsData);
    } catch (error) {
      console.error('Failed to load audit data:', error);
    } finally {
      setLoading(false);
    }
  }

  function formatTimestamp(ts: string) {
    return new Date(ts).toLocaleString();
  }

  return (
    <div className="container mx-auto p-6">
      <h1 className="text-3xl font-bold mb-6">Security Audit Dashboard</h1>
      
      {/* Statistics Cards */}
      {stats && (
        <div className="grid grid-cols-1 md:grid-cols-4 gap-4 mb-6">
          <Card>
            <CardContent className="pt-6">
              <div className="text-2xl font-bold">{stats.total_actions}</div>
              <p className="text-sm text-gray-600">Total Actions</p>
            </CardContent>
          </Card>
          
          <Card>
            <CardContent className="pt-6">
              <div className="text-2xl font-bold text-green-600">
                {stats.successful_actions}
              </div>
              <p className="text-sm text-gray-600">Successful</p>
            </CardContent>
          </Card>
          
          <Card>
            <CardContent className="pt-6">
              <div className="text-2xl font-bold text-red-600">
                {stats.failed_actions}
              </div>
              <p className="text-sm text-gray-600">Failed</p>
            </CardContent>
          </Card>
          
          <Card>
            <CardContent className="pt-6">
              <div className="text-2xl font-bold text-blue-600">
                {stats.success_rate}%
              </div>
              <p className="text-sm text-gray-600">Success Rate</p>
            </CardContent>
          </Card>
        </div>
      )}

      {/* Filters */}
      <Card className="mb-6">
        <CardHeader>
          <CardTitle>Filters</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="flex gap-4">
            <input
              type="text"
              placeholder="Filter by user"
              value={filter.user}
              onChange={(e) => setFilter({ ...filter, user: e.target.value })}
              className="px-3 py-2 border rounded"
            />
            <input
              type="text"
              placeholder="Filter by action"
              value={filter.action}
              onChange={(e) => setFilter({ ...filter, action: e.target.value })}
              className="px-3 py-2 border rounded"
            />
            <Button onClick={loadAuditData} variant="primary">
              Apply Filters
            </Button>
          </div>
        </CardContent>
      </Card>

      {/* Top Actions */}
      {stats && stats.top_actions.length > 0 && (
        <Card className="mb-6">
          <CardHeader>
            <CardTitle>Top Actions</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="space-y-2">
              {stats.top_actions.map((item, idx) => (
                <div key={idx} className="flex justify-between items-center">
                  <span className="font-mono text-sm">{item.action}</span>
                  <Badge variant="secondary">{item.count}</Badge>
                </div>
              ))}
            </div>
          </CardContent>
        </Card>
      )}

      {/* Audit Logs Table */}
      <Card>
        <CardHeader>
          <CardTitle>Audit Logs</CardTitle>
        </CardHeader>
        <CardContent>
          {loading ? (
            <p>Loading...</p>
          ) : logs.length === 0 ? (
            <p className="text-gray-500">No audit logs found</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="border-b">
                  <tr>
                    <th className="text-left p-2">Timestamp</th>
                    <th className="text-left p-2">User</th>
                    <th className="text-left p-2">Action</th>
                    <th className="text-left p-2">Resource</th>
                    <th className="text-left p-2">Status</th>
                  </tr>
                </thead>
                <tbody>
                  {logs.map((log, idx) => (
                    <tr key={idx} className="border-b hover:bg-gray-50">
                      <td className="p-2 text-xs">
                        {formatTimestamp(log.timestamp)}
                      </td>
                      <td className="p-2">{log.user}</td>
                      <td className="p-2 font-mono text-xs">{log.action}</td>
                      <td className="p-2 text-xs truncate max-w-xs" title={log.resource}>
                        {log.resource}
                      </td>
                      <td className="p-2">
                        <Badge variant={log.success ? 'success' : 'danger'}>
                          {log.success ? 'SUCCESS' : 'FAILED'}
                        </Badge>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
