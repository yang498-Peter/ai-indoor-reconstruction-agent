"""Create a tiny triangle GLB for viewer tests, containing no customer data."""
import json
import struct
from pathlib import Path

W=Path(__file__).resolve().parent
positions=struct.pack('<9f',-1.0,0.0,0.0,1.0,0.0,0.0,0.0,1.5,0.0)
indices=struct.pack('<3H',0,1,2)
binary=positions+indices+b'\0\0'
metadata={'asset':{'version':'2.0','generator':'synthetic test'},'buffers':[{'byteLength':len(binary)}],
          'bufferViews':[{'buffer':0,'byteOffset':0,'byteLength':36,'target':34962},
                         {'buffer':0,'byteOffset':36,'byteLength':6,'target':34963}],
          'accessors':[{'bufferView':0,'componentType':5126,'count':3,'type':'VEC3','min':[-1,0,0],'max':[1,1.5,0]},
                       {'bufferView':1,'componentType':5123,'count':3,'type':'SCALAR'}],
          'meshes':[{'primitives':[{'attributes':{'POSITION':0},'indices':1}]}],
          'nodes':[{'mesh':0}],'scenes':[{'nodes':[0]}],'scene':0}
j=json.dumps(metadata,separators=(',',':')).encode('utf-8');j+=b' '*((-len(j))%4)
data=struct.pack('<III',0x46546c67,2,12+8+len(j)+8+len(binary))+struct.pack('<II',len(j),0x4e4f534a)+j+struct.pack('<II',len(binary),0x004e4942)+binary
(W/'synthetic-model.glb').write_bytes(data)
print('Synthetic GLB:',len(data),'bytes')
