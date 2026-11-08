from src.testing.test_framework import AmmeterTestFramework

def main():
    # יצירת מסגרת הבדיקות
    framework = AmmeterTestFramework()
    
    # הרצת בדיקות לכל סוגי האמפרמטרים
    ammeter_types = ["greenlee", "entes", "circutor"]
    results = {}
    
    for ammeter_type in ammeter_types:
        print(f"Testing {ammeter_type} ammeter...")
        results[ammeter_type] = framework.run_test()
        
    # השוואת תוצאות
    for ammeter_type, result in results.items():
        print(f"\nResults for {ammeter_type}:")

if __name__ == "__main__":
    main() 