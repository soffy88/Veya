'use client';

import { useState, useEffect } from 'react';
import { Card, CardHeader, CardContent, CardTitle } from '@helios/oui';
import { Button, Badge } from '@helios/oui';

interface ModelInfo {
  name: string;
  description: string;
  versions: string[];
  default_version: string;
  path: string;
}

export default function ModelsPage() {
  const [models, setModels] = useState<ModelInfo[]>([]);
  const [selectedModel, setSelectedModel] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [selectedVersion, setSelectedVersion] = useState<string>('latest');

  useEffect(() => {
    loadModels();
  }, []);

  async function loadModels() {
    try {
      const res = await fetch('http://localhost:8000/models');
      const data = await res.json();
      setModels(data);
    } catch (error) {
      console.error('Failed to load models:', error);
    } finally {
      setLoading(false);
    }
  }

  async function loadModel(modelName: string, version: string) {
    try {
      const body = { name: modelName, version };
      const res = await fetch('http://localhost:8000/models/load', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      
      if (res.ok) {
        const result = await res.json();
        alert(`Loaded ${result.name}@${result.version}\nPath: ${result.path}`);
      }
    } catch (error) {
      console.error('Failed to load model:', error);
      alert('Failed to load model');
    }
  }

  return (
    <div className="container mx-auto p-6">
      <h1 className="text-3xl font-bold mb-6">Model Management</h1>
      
      <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
        {/* Models List */}
        <Card className="md:col-span-1">
          <CardHeader>
            <CardTitle>Available Models</CardTitle>
          </CardHeader>
          <CardContent>
            {loading ? (
              <p>Loading...</p>
            ) : models.length === 0 ? (
              <p className="text-gray-500">No models found</p>
            ) : (
              <ul className="space-y-2">
                {models.map((model) => (
                  <li key={model.name}>
                    <button
                      onClick={() => setSelectedModel(model.name)}
                      className={`w-full text-left p-3 rounded-lg transition-colors ${
                        selectedModel === model.name
                          ? 'bg-blue-100 border-2 border-blue-500'
                          : 'hover:bg-gray-100'
                      }`}
                    >
                      <div className="font-semibold">{model.name}</div>
                      <div className="text-sm text-gray-600 truncate">
                        {model.description}
                      </div>
                      <Badge variant="secondary" className="mt-1">
                        {model.versions.length} version{model.versions.length !== 1 ? 's' : ''}
                      </Badge>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </CardContent>
        </Card>

        {/* Model Details */}
        {selectedModel && (
          <Card className="md:col-span-2">
            <CardHeader>
              <CardTitle>{selectedModel}</CardTitle>
            </CardHeader>
            <CardContent>
              {(() => {
                const model = models.find(m => m.name === selectedModel);
                if (!model) return null;
                
                return (
                  <div className="space-y-4">
                    <div>
                      <h3 className="font-semibold mb-2">Description</h3>
                      <p className="text-gray-700">{model.description}</p>
                    </div>

                    <div>
                      <h3 className="font-semibold mb-2">Versions</h3>
                      <div className="flex flex-wrap gap-2 mb-4">
                        {model.versions.map((version) => (
                          <button
                            key={version}
                            onClick={() => setSelectedVersion(version)}
                            className={`px-3 py-1 rounded-full text-sm ${
                              selectedVersion === version
                                ? 'bg-blue-600 text-white'
                                : 'bg-gray-200 hover:bg-gray-300'
                            }`}
                          >
                            {version}
                          </button>
                        ))}
                      </div>
                    </div>

                    <div>
                      <h3 className="font-semibold mb-2">Metadata</h3>
                      <dl className="grid grid-cols-2 gap-2 text-sm">
                        <dt className="text-gray-600">Default Version:</dt>
                        <dd>{model.default_version}</dd>
                        
                        <dt className="text-gray-600">Path:</dt>
                        <dd className="truncate" title={model.path}>{model.path}</dd>
                      </dl>
                    </div>

                    <Button
                      onClick={() => loadModel(selectedModel, selectedVersion)}
                      className="mt-4"
                      variant="primary"
                    >
                      Load {selectedModel}@{selectedVersion}
                    </Button>
                  </div>
                );
              })()}
            </CardContent>
          </Card>
        )}
      </div>
    </div>
  );
}
