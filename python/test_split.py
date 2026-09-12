import sys
import os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), 'lib')))

import vvc_splitter

if __name__ == "__main__":
    input_vvc = os.path.abspath(os.path.join(os.path.dirname(__file__), "../VVCSoftware_VTM/experiments/subpic_official/official_2subpic.vvc"))
    output_dir = "subpic_split_test"
    
    print("Testing vvc_splitter.split()...")
    res = vvc_splitter.split(input_vvc, output_dir)
    print("\nResulting files:")
    for key, path in res.items():
        print(f"  {key}: {path}")
        
    print("\nTesting vvc_splitter.merge()...")
    merged_out = os.path.join(output_dir, "merged.vvc")
    vvc_splitter.merge(output_dir, merged_out)
    
    print("\nTest completed successfully!")
