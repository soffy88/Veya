'use client';

import { useState, useEffect } from 'react';
import { Card, CardHeader, CardContent, CardTitle } from '@helios/oui';
import { Button, Badge } from '@helios/oui';

interface Workflow {
  name: string;
  status: 'running' | 'success' | 'failed' | 'pending';
  started_at: string;
  duration?: string;
  steps: {
    name: string;
    status: 'running' | 'success' | 'failed' | 'pending';
    duration?: string;
  }[];
}

export default function CICDPage() {
  const [workflows, setWorkflows] = useState<Workflow[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    // Mock data for now - will connect to real CI/CD backend later
    const mockWorkflows: Workflow[] = [
      {
        name: 'CI/CD Pipeline',
        status: 'success',
        started_at: new Date(Date.now() - 300000).toISOString(),
        duration: '5m 23s',
        steps: [
          { name: 'checkout', status: 'success', duration: '2s' },
          { name: 'setup_python', status: 'success', duration: '15s' },
          { name: 'install_dependencies', status: 'success', duration: '45s' },
          { name: 'run_tests', status: 'success', duration: '3m 20s' },
          { name: 'build_and_deploy', status: 'success', duration: '1m 1s' }
        ]
      },
      {
        name: 'Security Scan',
        status: 'running',
        started_at: new Date(Date.now() - 120000).toISOString(),
        steps: [
          { name: 'checkout', status: 'success', duration: '2s' },
          { name: 'scan_dependencies', status: 'running' },
          { name: 'check_vulnerabilities', status: 'pending' },
          { name: 'generate_report', status: 'pending' }
        ]
      }
    ];
    
    setWorkflows(mockWorkflows);
    setLoading(false);
  }, []);

  function getStatusColor(status: string) {
    switch (status) {
      case 'success': return 'bg-green-500';
      case 'failed': return 'bg-red-500';
      case 'running': return 'bg-blue-500 animate-pulse';
      case 'pending': return 'bg-gray-300';
      default: return 'bg-gray-300';
    }
  }

  function formatTimestamp(ts: string) {
    return new Date(ts).toLocaleString();
  }

  return (
    <div className="container mx-auto p-6">
      <div className="flex justify-between items-center mb-6">
        <h1 className="text-3xl font-bold">CI/CD Pipeline Status</h1>
        <Button variant="primary" onClick={() => setLoading(true)}>
          Refresh
        </Button>
      </div>

      {loading ? (
        <p>Loading workflows...</p>
      ) : workflows.length === 0 ? (
        <Card>
          <CardContent className="pt-6">
            <p className="text-gray-500">No workflows found</p>
          </CardContent>
        </Card>
      ) : (
        <div className="space-y-6">
          {workflows.map((workflow, idx) => (
            <Card key={idx}>
              <CardHeader>
                <div className="flex justify-between items-center">
                  <CardTitle>{workflow.name}</CardTitle>
                  <div className="flex items-center gap-2">
                    <div className={`w-3 h-3 rounded-full ${getStatusColor(workflow.status)}`}></div>
                    <Badge variant={workflow.status === 'success' ? 'success' : workflow.status === 'failed' ? 'danger' : 'secondary'}>
                      {workflow.status.toUpperCase()}
                    </Badge>
                  </div>
                </div>
              </CardHeader>
              <CardContent>
                <div className="mb-4">
                  <p className="text-sm text-gray-600">
                    Started: {formatTimestamp(workflow.started_at)}
                    {workflow.duration && ` • Duration: ${workflow.duration}`}
                  </p>
                </div>

                <div className="space-y-2">
                  {workflow.steps.map((step, stepIdx) => (
                    <div key={stepIdx} className="flex items-center gap-3 p-2 bg-gray-50 rounded">
                      <div className={`w-2 h-2 rounded-full ${getStatusColor(step.status)}`}></div>
                      <span className="font-mono text-sm flex-1">{step.name}</span>
                      {step.duration && (
                        <span className="text-xs text-gray-600">{step.duration}</span>
                      )}
                    </div>
                  ))}
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}
